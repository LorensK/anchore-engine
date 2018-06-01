import copy
import os
import re
import threading
import time
import json
import traceback
import operator

import connexion
from twisted.application import internet
from twisted.internet import reactor
from twisted.internet.task import LoopingCall
from twisted.web.wsgi import WSGIResource
from twisted.web.resource import Resource
from twisted.web import rewrite

# anchore modules
from anchore_engine.clients import catalog, localanchore, simplequeue, localanchore_standalone
import anchore_engine.configuration.localconfig
import anchore_engine.subsys.servicestatus
import anchore_engine.subsys.metrics
import anchore_engine.services.common
import anchore_engine.subsys.taskstate
import anchore_engine.subsys.notifications
from anchore_engine.subsys import logger

import anchore_engine.clients.policy_engine
from anchore_engine.clients.policy_engine.generated.models import ImageIngressRequest

servicename = 'analyzer'
_default_api_version = "v1"

# service funcs (must be here)
def default_version_rewrite(request):
    global _default_api_version
    try:
        if request.postpath:
            if request.postpath[0] != 'health' and request.postpath[0] != _default_api_version:
                request.postpath.insert(0, _default_api_version)
                request.path = '/'+_default_api_version+request.path
    except Exception as err:
        logger.error("rewrite exception: " +str(err))
        raise err

def createService(sname, config):
    global monitor_threads, monitors, servicename

    try:
        application = connexion.FlaskApp(__name__, specification_dir='swagger/')
        flask_app = application.app
        flask_app.url_map.strict_slashes = False
        anchore_engine.subsys.metrics.init_flask_metrics(flask_app, servicename=servicename)
        application.add_api('swagger.yaml')
    except Exception as err:
        traceback.print_exc()
        raise err

    try:
        myconfig = config['services'][sname]
        servicename = sname
    except Exception as err:
        raise err

    try:
        kick_timer = int(myconfig['cycle_timer_seconds'])
    except:
        kick_timer = 1

    doapi = False
    try:
        if myconfig['listen'] and myconfig['port'] and myconfig['endpoint_hostname']:
            doapi = True
    except:
        doapi = False

    kwargs = {}
    kwargs['kick_timer'] = kick_timer
    kwargs['monitors'] = monitors
    kwargs['monitor_threads'] = monitor_threads
    kwargs['servicename'] = servicename

    if doapi:
        # start up flask service

        flask_site = WSGIResource(reactor, reactor.getThreadPool(), application=flask_app)
        realroot = Resource()
        realroot.putChild(b"v1", anchore_engine.services.common.getAuthResource(flask_site, sname, config))
        realroot.putChild(b"health", anchore_engine.services.common.HealthResource())
        # this will rewrite any calls that do not have an explicit version to the base path before being processed by flask
        root = rewrite.RewriterResource(realroot, default_version_rewrite)
        #root = anchore_engine.services.common.getAuthResource(flask_site, sname, config)
        ret_svc = anchore_engine.services.common.createServiceAPI(root, sname, config)

        # start up the monitor as a looping call
        lc = LoopingCall(anchore_engine.services.common.monitor, **kwargs)
        lc.start(1)
    else:
        # start up the monitor as a timer service
        svc = internet.TimerService(1, anchore_engine.services.common.monitor, **kwargs)
        svc.setName(sname)
        ret_svc = svc

    return (ret_svc)

def initializeService(sname, config):
    return (anchore_engine.services.common.initializeService(sname, config))

def registerService(sname, config):
    rc = anchore_engine.services.common.registerService(sname, config, enforce_unique=False)

    #service_record = {'hostid': config['host_id'], 'servicename': sname}
    service_record = anchore_engine.subsys.servicestatus.get_my_service_record()
    anchore_engine.subsys.servicestatus.set_status(service_record, up=True, available=True, update_db=True)

    return (rc)


############################################

queuename = "images_to_analyze"
system_user_auth = ('anchore-system', '')
#current_avg = 0.0
#current_avg_count = 0.0

def perform_analyze(userId, manifest, image_record, registry_creds, layer_cache_enable=False):
    global servicename

    localconfig = anchore_engine.configuration.localconfig.get_config()
    try:
        myconfig = localconfig['services'][servicename]
    except:
        myconfig = {}

    driver = 'localanchore'
    if 'analyzer_driver' in myconfig:
        driver = myconfig['analyzer_driver']

    if driver == 'nodocker':
        return(perform_analyze_nodocker(userId, manifest, image_record, registry_creds, layer_cache_enable=layer_cache_enable))
    else:
        if not os.path.exists("/usr/bin/anchore"):
            raise Exception("this build of anchore-engine does not include the local 'anchore' tool which is required for the 'localanchore' analyzer driver.  Please switch your analyzer driver to 'nodocker' mode and restart the service to proceed")
        return(perform_analyze_localanchore(userId, manifest, image_record, registry_creds, layer_cache_enable=layer_cache_enable))

def perform_analyze_nodocker(userId, manifest, image_record, registry_creds, layer_cache_enable=False):
    ret_analyze = {}
    ret_query = {}

    localconfig = anchore_engine.configuration.localconfig.get_config()
    try:
        tmpdir = localconfig['tmp_dir']
    except Exception as err:
        logger.warn("could not get tmp_dir from localconfig - exception: " + str(err))
        tmpdir = "/tmp"

    use_cache_dir=None
    if layer_cache_enable:
        use_cache_dir = os.path.join(tmpdir, "anchore_layercache")

    # choose the first TODO possible more complex selection here
    try:
        image_detail = image_record['image_detail'][0]
        registry_manifest = manifest
        pullstring = image_detail['registry'] + "/" + image_detail['repo'] + "@" + image_detail['imageDigest']
        fulltag = image_detail['registry'] + "/" + image_detail['repo'] + ":" + image_detail['tag']
        logger.debug("using pullstring ("+str(pullstring)+") and fulltag ("+str(fulltag)+") to pull image data")
    except Exception as err:
        image_detail = pullstring = fulltag = None
        raise Exception("failed to extract requisite information from image_record - exception: " + str(err))
        
    timer = int(time.time())
    logger.spew("TIMING MARK0: " + str(int(time.time()) - timer))
    logger.info("performing analysis on image: " + str([userId, pullstring, fulltag]))

    logger.debug("obtaining anchorelock..." + str(pullstring))
    with localanchore.get_anchorelock(lockId=pullstring, driver='nodocker'):
        logger.debug("obtaining anchorelock successful: " + str(pullstring))
        analyzed_image_report = localanchore_standalone.analyze_image(userId, registry_manifest, image_record, tmpdir, localconfig, registry_creds=registry_creds, use_cache_dir=use_cache_dir)
        ret_analyze = analyzed_image_report

    logger.info("performing analysis on image complete: " + str(pullstring))

    return (ret_analyze)

def perform_analyze_localanchore(userId, manifest, image_record, registry_creds, layer_cache_enable=False):
    ret_analyze = {}

    localconfig = anchore_engine.configuration.localconfig.get_config()
    do_docker_cleanup = localconfig['cleanup_images']

    try:
        image_detail = image_record['image_detail'][0]
        registry_manifest = manifest
        pullstring = image_detail['registry'] + "/" + image_detail['repo'] + "@" + image_detail['imageDigest']
        fulltag = image_detail['registry'] + "/" + image_detail['repo'] + ":" + image_detail['tag']
        logger.debug("using pullstring ("+str(pullstring)+") and fulltag ("+str(fulltag)+") to pull image data")
    except Exception as err:
        image_detail = pullstring = fulltag = None
        raise Exception("failed to extract requisite information from image_record - exception: " + str(err))


    timer = int(time.time())
    logger.spew("TIMING MARK0: " + str(int(time.time()) - timer))
    logger.debug("obtaining anchorelock..." + str(pullstring))
    with localanchore.get_anchorelock(lockId=pullstring):
        logger.debug("obtaining anchorelock successful: " + str(pullstring))

        logger.spew("TIMING MARK1: " + str(int(time.time()) - timer))
        logger.info("performing analysis on image: " + str(pullstring))

        # pull the digest, but also any tags associated with the image (that we know of) in order to populate the local docker image
        try:
            rc = localanchore.pull(userId, pullstring, image_detail, pulltags=True,
                                                          registry_creds=registry_creds)
            if not rc:
                raise Exception("anchore analyze failed:")
            pullstring = re.sub("sha256:", "", rc['Id'])
            image_detail['imageId'] = pullstring
        except Exception as err:
            logger.error("error on pull: " + str(err))
            raise err

        logger.spew("TIMING MARK2: " + str(int(time.time()) - timer))

        # analyze!
        try:
            rc = localanchore.analyze(pullstring, image_detail)
            if not rc:
                raise Exception("anchore analyze failed:")
        except Exception as err:
            logger.error("error on analyze: " + str(err))
            raise err

        logger.spew("TIMING MARK3: " + str(int(time.time()) - timer))

        # get the result from anchore
        logger.debug("retrieving image data from anchore")
        try:
            image_data = localanchore.get_image_export(pullstring, image_detail)
            if not image_data:
                raise Exception("anchore image data export failed:")
        except Exception as err:
            logger.error("error on image export: " + str(err))
            raise err

        logger.spew("TIMING MARK5: " + str(int(time.time()) - timer))

        try:
            logger.debug("removing image: " + str(pullstring))
            rc = localanchore.remove_image(pullstring, docker_remove=do_docker_cleanup,
                                                                  anchore_remove=True)
            logger.debug("removing image complete: " + str(pullstring))
        except Exception as err:
            raise err

        logger.spew("TIMING MARK6: " + str(int(time.time()) - timer))

    ret_analyze = image_data

    logger.info("performing analysis on image complete: " + str(pullstring))
    return (ret_analyze)

def process_analyzer_job(system_user_auth, qobj, layer_cache_enable):
    global servicename #current_avg, current_avg_count

    timer = int(time.time())
    try:
        logger.debug('dequeued object: {}'.format(qobj))

        record = qobj['data']
        userId = record['userId']
        imageDigest = record['imageDigest']
        manifest = record['manifest']

        user_record = catalog.get_user(system_user_auth, userId)
        user_auth = (user_record['userId'], user_record['password'])

        # check to make sure image is still in DB
        try:
            image_records = catalog.get_image(user_auth, imageDigest=imageDigest)
            if image_records:
                image_record = image_records[0]
            else:
                raise Exception("empty image record from catalog")
        except Exception as err:
            logger.warn("dequeued image cannot be fetched from catalog - skipping analysis (" + str(
                imageDigest) + ") - exception: " + str(err))
            return (True)

        logger.info("image dequeued for analysis: " + str(userId) + " : " + str(imageDigest))
        if image_record['analysis_status'] != anchore_engine.subsys.taskstate.base_state('analyze'):
            logger.debug("dequeued image is not in base state - skipping analysis")
            return(True)
        
        try:
            logger.spew("TIMING MARK0: " + str(int(time.time()) - timer))

            last_analysis_status = image_record['analysis_status']
            image_record['analysis_status'] = anchore_engine.subsys.taskstate.working_state('analyze')
            rc = catalog.update_image(user_auth, imageDigest, image_record)

            # disable the webhook call for image state transistion to 'analyzing'
            #try:
            #    for image_detail in image_record['image_detail']:
            #        fulltag = image_detail['registry'] + "/" + image_detail['repo'] + ":" + image_detail['tag']
            #        npayload = {
            #            'last_eval': {'imageDigest': imageDigest, 'analysis_status': last_analysis_status},
            #            'curr_eval': {'imageDigest': imageDigest, 'analysis_status': image_record['analysis_status']},
            #        }
            #        rc = anchore_engine.subsys.notifications.queue_notification(userId, fulltag, 'analysis_update', npayload)
            #except Exception as err:
            #    logger.warn("failed to enqueue notification on image analysis state update - exception: " + str(err))

            # actually do analysis
            registry_creds = catalog.get_registry(user_auth)
            image_data = perform_analyze(userId, manifest, image_record, registry_creds, layer_cache_enable=layer_cache_enable)

            imageId = None
            try:
                imageId = image_data[0]['image']['imageId']
            except Exception as err:
                logger.warn("could not get imageId after analysis or from image record - exception: " + str(err))

            logger.debug("archiving analysis data")
            rc = catalog.put_document(user_auth, 'analysis_data', imageDigest, image_data)

            if rc:
                try:
                    logger.debug("extracting image content data")
                    image_content_data = {}
                    for content_type in anchore_engine.services.common.image_content_types + anchore_engine.services.common.image_metadata_types:
                        try:
                            image_content_data[content_type] = anchore_engine.services.common.extract_analyzer_content(image_data, content_type, manifest=manifest)
                        except:
                            image_content_data[content_type] = {}

                    if image_content_data:
                        logger.debug("adding image content data to archive")
                        rc = catalog.put_document(user_auth, 'image_content_data', imageDigest, image_content_data)

                    try:
                        logger.debug("adding image analysis data to image_record")
                        anchore_engine.services.common.update_image_record_with_analysis_data(image_record, image_data)

                    except Exception as err:
                        raise err

                except Exception as err:
                    logger.warn("could not store image content metadata to archive - exception: " + str(err))

                logger.debug("adding image record to policy-engine service (" + str(userId) + " : " + str(imageId) + ")")
                try:
                    if not imageId:
                        raise Exception("cannot add image to policy engine without an imageId")

                    localconfig = anchore_engine.configuration.localconfig.get_config()
                    verify = localconfig['internal_ssl_verify']

                    client = anchore_engine.clients.policy_engine.get_client(user=system_user_auth[0], password=system_user_auth[1], verify_ssl=verify)

                    try:
                        logger.debug("clearing any existing record in policy engine for image: " + str(imageId))
                        rc = client.delete_image(user_id=userId, image_id=imageId)
                    except Exception as err:
                        logger.warn("exception on pre-delete - exception: " + str(err))

                    logger.info('Loading image: {} {}'.format(userId, imageId))
                    request = ImageIngressRequest(user_id=userId, image_id=imageId, fetch_url='catalog://'+str(userId)+'/analysis_data/'+str(imageDigest))
                    logger.debug("policy engine request: " + str(request))
                    resp = client.ingress_image(request)
                    logger.debug("policy engine image add response: " + str(resp))

                except Exception as err:
                    import traceback
                    traceback.print_exc()
                    raise Exception("adding image to policy-engine failed - exception: " + str(err))

                logger.debug("updating image catalog record analysis_status")
                
                last_analysis_status = image_record['analysis_status']
                image_record['analysis_status'] = anchore_engine.subsys.taskstate.complete_state('analyze')
                image_record['analyzed_at'] = int(time.time())
                rc = catalog.update_image(user_auth, imageDigest, image_record)

                try:
                    annotations = {}
                    try:
                        if image_record.get('annotations', '{}'):
                            annotations = json.loads(image_record.get('annotations', '{}'))
                    except Exception as err:
                        logger.warn("could not marshal annotations from json - exception: " + str(err))

                    for image_detail in image_record['image_detail']:
                        fulltag = image_detail['registry'] + "/" + image_detail['repo'] + ":" + image_detail['tag']
                        last_payload = {'imageDigest': imageDigest, 'analysis_status': last_analysis_status, 'annotations': annotations}
                        curr_payload = {'imageDigest': imageDigest, 'analysis_status': image_record['analysis_status'], 'annotations': annotations}
                        npayload = {
                            'last_eval': last_payload,
                            'curr_eval': curr_payload,
                        }
                        if annotations:
                            npayload['annotations'] = annotations

                        rc = anchore_engine.subsys.notifications.queue_notification(userId, fulltag, 'analysis_update', npayload)
                except Exception as err:
                    logger.warn("failed to enqueue notification on image analysis state update - exception: " + str(err))

            else:
                raise Exception("analysis archive failed to store")

            logger.info("analysis complete: " + str(userId) + " : " + str(imageDigest))

            logger.spew("TIMING MARK1: " + str(int(time.time()) - timer))

            try:
                run_time = float(time.time() - timer)
                #current_avg_count = current_avg_count + 1.0
                #new_avg = current_avg + ((run_time - current_avg) / current_avg_count)
                #current_avg = new_avg

                anchore_engine.subsys.metrics.histogram_observe('anchore_analysis_time_seconds', run_time, buckets=[1.0, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0, 600.0, 1800.0, 3600.0], status="success")
                #anchore_engine.subsys.metrics.counter_inc('anchore_images_analyzed_total')

                #localconfig = anchore_engine.configuration.localconfig.get_config()
                #service_record = {'hostid': localconfig['host_id'], 'servicename': servicename}
                #anchore_engine.subsys.servicestatus.set_status(service_record, up=True, available=True, detail={'avg_analysis_time_sec': current_avg, 'total_analysis_count': current_avg_count}, update_db=True)

            except Exception as err:
                logger.warn(str(err))
                pass

        except Exception as err:
            run_time = float(time.time() - timer)
            logger.exception("problem analyzing image - exception: " + str(err))
            anchore_engine.subsys.metrics.histogram_observe('anchore_analysis_time_seconds', run_time, buckets=[1.0, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0, 600.0, 1800.0, 3600.0], status="fail")
            image_record['analysis_status'] = anchore_engine.subsys.taskstate.fault_state('analyze')
            image_record['image_status'] = anchore_engine.subsys.taskstate.fault_state('image_status')
            rc = catalog.update_image(user_auth, imageDigest, image_record)

    except Exception as err:
        logger.warn("job processing bailed - exception: " + str(err))
        raise err

    return (True)

def handle_layer_cache():

    try:
        localconfig = anchore_engine.configuration.localconfig.get_config()
        myconfig = localconfig['services']['analyzer']

        cachemax_gbs = int(myconfig.get('layer_cache_max_gigabytes', 1))
        cachemax = cachemax_gbs * 1000000000

        try:
            tmpdir = localconfig['tmp_dir']
        except Exception as err:
            logger.warn("could not get tmp_dir from localconfig - exception: " + str(err))
            tmpdir = "/tmp"
        use_cache_dir = os.path.join(tmpdir, "anchore_layercache")
        if os.path.exists(use_cache_dir):
            totalsize = 0
            layertimes = {}
            layersizes = {}
            try:
                for f in os.listdir(os.path.join(use_cache_dir, 'sha256')):
                    layerfile = os.path.join(use_cache_dir, 'sha256', f)
                    layerstat = os.stat(layerfile)
                    totalsize = totalsize + layerstat.st_size
                    layersizes[layerfile] = layerstat.st_size
                    layertimes[layerfile] = max([layerstat.st_mtime, layerstat.st_ctime, layerstat.st_atime])
                    
                if totalsize > cachemax:
                    logger.debug("layer cache total size ("+str(totalsize)+") exceeds configured cache max ("+str(cachemax)+") - performing cleanup")
                    currsize = totalsize
                    sorted_layers = sorted(layertimes.items(), key=operator.itemgetter(1))
                    while(currsize > cachemax):
                        rmlayer = sorted_layers.pop(0)
                        logger.debug("removing cached layer: " + str(rmlayer))
                        os.remove(rmlayer[0])
                        currsize = currsize - layersizes[rmlayer[0]]
                        logger.debug("currsize after remove: " + str(currsize))

            except Exception as err:
                raise(err)
        
    except Exception as err:
        raise(err)

    return(True)

def handle_image_analyzer(*args, **kwargs):
    global system_user_auth, queuename, servicename

    cycle_timer = kwargs['mythread']['cycle_timer']

    localconfig = anchore_engine.configuration.localconfig.get_config()
    system_user_auth = localconfig['system_user_auth']

    threads = []
    layer_cache_dirty = True
    while(True):
        logger.debug("analyzer thread cycle start")
        try:
            myconfig = localconfig['services']['analyzer']
            max_analyze_threads = int(myconfig.get('max_threads', 1))
            layer_cache_enable = myconfig.get('layer_cache_enable', False)

            logger.debug("max threads: " + str(max_analyze_threads))

            if len(threads) < max_analyze_threads:
                logger.debug("analyzer has free worker threads {} / {}".format(len(threads), max_analyze_threads))
                qobj = simplequeue.dequeue(system_user_auth, queuename)
                if qobj:
                    logger.debug("got work from queue task Id: {}".format(qobj.get('queueId', 'unknown')))
                    myqobj = copy.deepcopy(qobj)
                    logger.spew("incoming queue object: " + str(myqobj))
                    logger.debug("incoming queue task: " + str(myqobj.keys()))
                    logger.debug("starting thread")
                    athread = threading.Thread(target=process_analyzer_job, args=(system_user_auth, myqobj,layer_cache_enable))
                    athread.start()
                    threads.append(athread)
                    logger.debug("thread started")
                    layer_cache_dirty = True
                else:
                    logger.debug("analyzer queue is empty - no work this cycle")
            else:
                logger.debug("all workers are busy")

            alive_threads = []
            while(threads):
                athread = threads.pop()
                if not athread.isAlive():
                    try:
                        logger.debug("thread completed - joining")
                        athread.join()
                        logger.debug("thread joined")
                    except Exception as err:
                        logger.warn("cannot join thread - exception: " + str(err))
                else:
                    alive_threads.append(athread)
            threads = alive_threads

            if layer_cache_enable and layer_cache_dirty and len(threads) == 0:
                logger.debug("running layer cache handler")
                try:
                    handle_layer_cache()
                    layer_cache_dirty = False
                except Exception as err:
                    logger.warn("layer cache management failed - exception: " + str(err))

        except Exception as err:
            import traceback
            traceback.print_exc()
            logger.error(str(err))

        logger.debug("analyzer thread cycle complete: next in "+str(cycle_timer))
        time.sleep(cycle_timer)
    return(True)

def handle_metrics(*args, **kwargs):

    cycle_timer = kwargs['mythread']['cycle_timer']
    while(True):
        try:
            localconfig = anchore_engine.configuration.localconfig.get_config()
            try:
                tmpdir = localconfig['tmp_dir']
                svfs = os.statvfs(tmpdir)
                available_bytes = svfs.f_bsize * svfs.f_bavail
                anchore_engine.subsys.metrics.gauge_set("anchore_tmpspace_available_bytes", available_bytes)
            except Exception as err:
                logger.warn("unable to detect available bytes probe - exception: " + str(err))
        except Exception as err:
            logger.warn("handler failed - exception: " + str(err))

        time.sleep(cycle_timer)

    return(True)

# monitor infrastructure

monitors = {
    'service_heartbeat': {'handler': anchore_engine.subsys.servicestatus.handle_service_heartbeat, 'taskType': 'handle_service_heartbeat', 'args': [servicename], 'cycle_timer': 60, 'min_cycle_timer': 60, 'max_cycle_timer': 60, 'last_queued': 0, 'last_return': False, 'initialized': False},
    'image_analyzer': {'handler': handle_image_analyzer, 'taskType': 'handle_image_analyzer', 'args': [], 'cycle_timer': 1, 'min_cycle_timer': 1, 'max_cycle_timer': 120, 'last_queued': 0, 'last_return': False, 'initialized': False},
    'handle_metrics': {'handler': handle_metrics, 'taskType': 'handle_metrics', 'args': [servicename], 'cycle_timer': 15, 'min_cycle_timer': 15, 'max_cycle_timer': 15, 'last_queued': 0, 'last_return': False, 'initialized': False},
}
monitor_threads = {}
