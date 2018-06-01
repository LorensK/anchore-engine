import json
import os
import copy
import threading
import time
import traceback
import uuid

import connexion
from twisted.internet import reactor
from twisted.web.wsgi import WSGIResource
from twisted.web.resource import Resource
from twisted.web import rewrite
from twisted.internet.task import LoopingCall

# anchore modules
from anchore_engine.clients import http, simplequeue, policy_engine
from anchore_engine.clients.policy_engine.generated.rest import ApiException
import anchore_engine.configuration.localconfig
import anchore_engine.subsys.servicestatus
import anchore_engine.subsys.metrics
import anchore_engine.services.common
import anchore_engine.clients.common
from anchore_engine import db
from anchore_engine.db import db_catalog_image, db_eventlog, db_policybundle, db_policyeval, db_queues, db_registries, db_subscriptions, db_users, db_anchore, db_services
from anchore_engine.subsys import notifications, taskstate, logger, archive
from anchore_engine.services.catalog import catalog_impl
import anchore_engine.auth.anchore_io

servicename = 'catalog'
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
    global servicename

    try:
        application = connexion.FlaskApp(__name__, specification_dir='swagger/')
        flask_app = application.app
        flask_app.url_map.strict_slashes = False
        anchore_engine.subsys.metrics.init_flask_metrics(flask_app, servicename=servicename)
        application.add_api('swagger.yaml')
    except Exception as err:
        traceback.print_exc()
        raise err

    flask_site = WSGIResource(reactor, reactor.getThreadPool(), application=flask_app)
    realroot = Resource()
    realroot.putChild(b"v1", anchore_engine.services.common.getAuthResource(flask_site, sname, config))
    realroot.putChild(b"health", anchore_engine.services.common.HealthResource())
    # this will rewrite any calls that do not have an explicit version to the base path before being processed by flask
    root = rewrite.RewriterResource(realroot, default_version_rewrite)
    #root = anchore_engine.services.common.getAuthResource(flask_site, sname, config)
    return(anchore_engine.services.common.createServiceAPI(root, sname, config))

def initializeService(sname, config):
    try:
        archive.initialize(config['services'][sname])
    except Exception as err:
        logger.exception("Error initializing archive: check catalog configuration")
        raise err

    # set up defaults for users if not yet set up
    try:
        with db.session_scope() as dbsession:
            user_records = db_users.get_all(session=dbsession)
            for user_record in user_records:
                userId = user_record['userId']
                if userId == 'anchore-system':
                    continue

                bundle_records = db_policybundle.get_all_byuserId(userId, session=dbsession)
                if not bundle_records:
                    logger.debug("user has no policy bundle - installing default: " +str(userId))
                    localconfig = anchore_engine.configuration.localconfig.get_config()
                    if 'default_bundle_file' in localconfig and os.path.exists(localconfig['default_bundle_file']):
                        logger.info("loading def bundle: " + str(localconfig['default_bundle_file']))
                        try:
                            default_bundle = {}
                            with open(localconfig['default_bundle_file'], 'r') as FH:
                                default_bundle = json.loads(FH.read())
                            if default_bundle:
                                bundle_url = archive.put_document(userId, 'policy_bundles', default_bundle['id'], default_bundle)
                                policy_record = anchore_engine.services.common.make_policy_record(userId, default_bundle, active=True)
                                rc = db_policybundle.add(policy_record['policyId'], userId, True, policy_record, session=dbsession)
                                if not rc:
                                    raise Exception("policy bundle DB add failed")
                        except Exception as err:
                            logger.error("could not load up default bundle for user - exception: " + str(err))
    except Exception as err:
        raise Exception ("unable to initialize default user data - exception: " + str(err))

    # set up monitor
    try:
        kick_timer = int(config['services'][sname]['cycle_timer_seconds'])
    except:
        kick_timer = 1
    try:
        cycle_timers = {}
        cycle_timers.update(config['services'][sname]['cycle_timers'])
    except:
        cycle_timers = {}

    kwargs = {
        'kick_timer':kick_timer,
        'cycle_timers': cycle_timers
    }

    lc = LoopingCall(monitor, **kwargs)
    lc.start(1)

    return(anchore_engine.services.common.initializeService(sname, config))

def registerService(sname, config):
    rc = anchore_engine.services.common.registerService(sname, config, enforce_unique=False)

    service_record = anchore_engine.subsys.servicestatus.get_my_service_record()
    anchore_engine.subsys.servicestatus.set_status(service_record, up=True, available=True, update_db=True)

    return (rc)

##########################################################

# monitor section

def handle_vulnerability_scan(*args, **kwargs):
    global feed_sync_updated

    watcher = str(kwargs['mythread']['taskType'])
    handler_success = True
    
    timer = time.time()
    logger.debug("FIRING: " + str(watcher))

    try:
        all_ready = anchore_engine.clients.common.check_services_ready(['policy_engine'])
        if not all_ready:
            logger.debug("FIRING DONE: feed syncer (skipping due to required services not being available)")
            try:
                kwargs['mythread']['last_return'] = False
            except:
                pass
            return(True)

        with db.session_scope() as dbsession:
            users = db_users.get_all(session=dbsession)

        for user in users:
            userId = user['userId']
            if userId == 'anchore-system':
                continue

            # vulnerability scans

            doperform = False
            vuln_sub_tags = []
            for subscription_type in ['vuln_update']:
                dbfilter = {'subscription_type': subscription_type}
                with db.session_scope() as dbsession:
                    subscription_records = db_subscriptions.get_byfilter(userId, session=dbsession, **dbfilter)
                for subscription_record in subscription_records:
                    if subscription_record['active']:
                        image_info = anchore_engine.services.common.get_image_info(userId, "docker", subscription_record['subscription_key'], registry_lookup=False, registry_creds=(None, None))
                        dbfilter = {'registry': image_info['registry'], 'repo': image_info['repo'], 'tag': image_info['tag']}
                        if dbfilter not in vuln_sub_tags:
                            vuln_sub_tags.append(dbfilter)

            for dbfilter in vuln_sub_tags:
                with db.session_scope() as dbsession:
                    image_records = db_catalog_image.get_byimagefilter(userId, 'docker', dbfilter=dbfilter, onlylatest=True, session=dbsession)
                for image_record in image_records:
                    if image_record['analysis_status'] == taskstate.complete_state('analyze'):
                        imageDigest = image_record['imageDigest']
                        fulltag = dbfilter['registry'] + "/" + dbfilter['repo'] + ":" + dbfilter['tag']

                        doperform = True
                        if doperform:
                            logger.debug("calling vuln scan perform: " + str(fulltag) + " : " + str(imageDigest))
                            with db.session_scope() as dbsession:
                                try:
                                    rc = catalog_impl.perform_vulnerability_scan(userId, imageDigest, dbsession, scantag=fulltag, force_refresh=False)
                                except Exception as err:
                                    logger.warn("vulnerability scan failed - exception: " + str(err))

    except Exception as err:
        logger.warn("failure in feed sync handler - exception: " + str(err))


    logger.debug("FIRING DONE: " + str(watcher))
    try:
        kwargs['mythread']['last_return'] = handler_success
    except:
        pass

    if anchore_engine.subsys.metrics.is_enabled() and handler_success:
        anchore_engine.subsys.metrics.summary_observe('anchore_monitor_runtime_seconds', time.time() - timer, function=watcher, status="success")
    else:
        anchore_engine.subsys.metrics.summary_observe('anchore_monitor_runtime_seconds', time.time() - timer, function=watcher, status="fail")

    return(True)

def handle_service_watcher(*args, **kwargs):
    #global latest_service_records

    cycle_timer = kwargs['mythread']['cycle_timer']
    max_service_heartbeat_timer = 300
    max_service_orphaned_timer = 3600

    while(True):
        logger.debug("FIRING: service watcher")

        localconfig = anchore_engine.configuration.localconfig.get_config()
        verify = localconfig['internal_ssl_verify']

        with db.session_scope() as dbsession:
            system_user = db_users.get('anchore-system', session=dbsession)
            userId = system_user['userId']
            password = system_user['password']

            anchore_services = db_services.get_all(session=dbsession)
            # update the global latest service record dict in services.common
            #latest_service_records.update({"service_records": copy.deepcopy(anchore_services)})

            # fields to update each tick:
            #
            # heartbeat (current time)
            # status (true/false)
            # status_message (state of service)
            # short_description(api return)
            #

            for service in anchore_services:
                service_update_record = {}
                if service['servicename'] == 'catalog' and service['hostid'] == localconfig['host_id']:
                    status = anchore_engine.subsys.servicestatus.get_status(service)
                    service_update_record.update({'heartbeat': int(time.time()), 'status': True, 'status_message': taskstate.complete_state('service_status'), 'short_description': json.dumps(status)})
                else:
                    try:
                        try:
                            status = json.loads(service['short_description'])
                        except:
                            status = {'up': False, 'available': False}
                            
                        # set to down until the response can be parsed
                        service_update_record['status'] = False
                        service_update_record['status_message'] = taskstate.fault_state('service_status')
                        service_update_record['short_description'] = "could not get service status description"

                        try:
                            # NOTE: this is where any service-specific decisions based on the 'status' record could happen - now all services are the same
                            if status['up'] and status['available']:
                                if time.time() - service['heartbeat'] > max_service_heartbeat_timer:
                                    logger.warn("no service heartbeat within allowed time period ("+str([service['hostid'], service['base_url']]) + " - disabling service")
                                    service_update_record['short_description'] = "no heartbeat from service in ({}) seconds".format(max_service_heartbeat_timer)
                                else:
                                    service_update_record['status'] = True
                                    service_update_record['status_message'] = taskstate.complete_state('service_status')
                                    try:
                                        service_update_record['short_description'] = json.dumps(status)
                                    except:
                                        service_update_record['short_description'] = str(status)
                            else:
                                if time.time() - service['heartbeat'] > max_service_orphaned_timer:
                                    logger.warn("no service heartbeat within max allowed time period ("+str([service['hostid'], service['base_url']]) + " - orphaning service")
                                    service_update_record['status'] = False
                                    service_update_record['status_message'] = taskstate.orphaned_state('service_status')
                                    service_update_record['short_description'] = "no heartbeat from service in ({}) seconds".format(max_service_orphaned_timer)

                        except Exception as err:
                            logger.warn("could not get/parse service status record for service: - exception: " + str(err))

                    except Exception as err:
                        logger.warn("could not get service status: " + str(service) + " : exception: " + str(err) + " : " + str(err.__dict__))
                        service_update_record['status'] = False
                        service_update_record['status_message'] = taskstate.fault_state('service_status')
                        service_update_record['short_description'] = "could not get service status"

                if service_update_record:
                    service.update(service_update_record)
                    try:
                        db_services.update_record(service, session=dbsession)
                    except Exception as err:
                        logger.warn("could not update DB: " + str(err))
                else:
                    logger.warn("no service_update_record populated - nothing to update")

        logger.debug("FIRING DONE: service watcher")
        try:
            kwargs['mythread']['last_return'] = True
        except:
            pass

        time.sleep(cycle_timer)
    return(True)

def handle_repo_watcher(*args, **kwargs):
    global system_user_auth

    watcher = str(kwargs['mythread']['taskType'])
    handler_success = True
    
    timer = time.time()
    logger.debug("FIRING: " + str(watcher))

    with db.session_scope() as dbsession:
        users = db_users.get_all(session=dbsession)

    for user in users:
        userId = user['userId']
        if userId == 'anchore-system':
            continue

        dbfilter = {}
        with db.session_scope() as dbsession:
            dbfilter['subscription_type'] = 'repo_update'
            subscription_records = db_subscriptions.get_byfilter(userId, session=dbsession, **dbfilter)

            registry_creds = db_registries.get_byuserId(userId, session=dbsession)
            try:
                catalog_impl.refresh_registry_creds(registry_creds, dbsession)
            except Exception as err:
                logger.warn("failed to refresh registry credentials - exception: " + str(err))

        for subscription_record in subscription_records:
            if not subscription_record['active']:
                continue

            try:
                regrepo = subscription_record['subscription_key']
                if subscription_record['subscription_value']:
                    subscription_value = json.loads(subscription_record['subscription_value'])
                    if 'autosubscribe' not in subscription_value:
                        subscription_value['autosubscribe'] = False
                    if 'lookuptag' not in subscription_value:
                        subscription_value['lookuptag'] = 'latest'
                    
                else:
                    subscription_value = {'autosubscribe': False, 'lookuptag': 'latest'}

                stored_repotags = subscription_value.get('repotags', [])

                fulltag = regrepo + ":" + subscription_value.get('lookuptag', 'latest')
                image_info = anchore_engine.services.common.get_image_info(userId, "docker", fulltag, registry_lookup=False, registry_creds=(None, None))
                curr_repotags = anchore_engine.auth.docker_registry.get_repo_tags(userId, image_info, registry_creds=registry_creds)

                autosubscribes = ['analysis_update']
                if subscription_value['autosubscribe']:
                    autosubscribes.append("tag_update")

                repotags = set(curr_repotags).difference(set(stored_repotags))
                if repotags:
                    logger.debug("new tags to watch in repo ("+str(regrepo)+"): " + str(repotags))
                    added_repotags = stored_repotags
                    
                    for repotag in repotags:
                        try:
                            fulltag = image_info['registry'] + "/" + image_info['repo'] + ":" + repotag
                            logger.debug("found new tag in repo: " + str(fulltag))
                            new_image_info = anchore_engine.services.common.get_image_info(userId, "docker", fulltag, registry_lookup=True, registry_creds=registry_creds)
                            manifest = None
                            try:
                                if 'manifest' in new_image_info:
                                    manifest = json.dumps(new_image_info['manifest'])
                                else:
                                    raise Exception("no manifest from get_image_info")
                            except Exception as err:
                                raise Exception("could not fetch/parse manifest - exception: " + str(err))

                            with db.session_scope() as dbsession:
                                logger.debug("adding/updating image from repo scan " + str(new_image_info['fulltag']))

                                # add the image
                                image_records = catalog_impl.add_or_update_image(dbsession, userId, new_image_info['imageId'], tags=[new_image_info['fulltag']], digests=[new_image_info['fulldigest']], manifest=manifest)
                                # add the subscription records with the configured default activations

                                for stype in anchore_engine.services.common.subscription_types:
                                    activate = False
                                    if stype == 'repo_update':
                                        continue
                                    elif stype in autosubscribes:
                                        activate = True
                                    db_subscriptions.add(userId, new_image_info['fulltag'], stype, {'active': activate}, session=dbsession)

                            added_repotags.append(repotag)
                        except Exception as err:
                            logger.warn("could not add discovered tag from repo ("+str(fulltag)+") - exception: " + str(err))

                    # update the subscription record with the latest successfully added image tags
                    with db.session_scope() as dbsession:
                        subscription_value['repotags'] = added_repotags
                        subscription_value['tagcount'] = len(added_repotags)
                        db_subscriptions.update(userId, regrepo, 'repo_update', {'subscription_value': json.dumps(subscription_value)}, session=dbsession)

                else:
                    logger.debug("no new images in watched repo ("+str(regrepo)+"): skipping")
            except Exception as err:
                logger.warn("failed to process repo_update subscription - exception: " + str(err))

    logger.debug("FIRING DONE: " + str(watcher))
    try:
        kwargs['mythread']['last_return'] = handler_success
    except:
        pass

    if anchore_engine.subsys.metrics.is_enabled() and handler_success:
        anchore_engine.subsys.metrics.summary_observe('anchore_monitor_runtime_seconds', time.time() - timer, function=watcher, status="success")
    else:
        anchore_engine.subsys.metrics.summary_observe('anchore_monitor_runtime_seconds', time.time() - timer, function=watcher, status="fail")

    return(True)

def handle_image_watcher(*args, **kwargs):
    global system_user_auth

    watcher = str(kwargs['mythread']['taskType'])
    handler_success = True
    
    timer = time.time()
    logger.debug("FIRING: " + str(watcher))
    with db.session_scope() as dbsession:
        users = db_users.get_all(session=dbsession)

    for user in users:
        userId = user['userId']
        if userId == 'anchore-system':
            continue

        with db.session_scope() as dbsession:
            dbfilter = {}
            dbfilter['subscription_type'] = 'tag_update'
            subscription_records = db_subscriptions.get_byfilter(userId, session=dbsession, **dbfilter)

            registry_creds = db_registries.get_byuserId(userId, session=dbsession)
            try:
                catalog_impl.refresh_registry_creds(registry_creds, dbsession)
            except Exception as err:
                logger.warn("failed to refresh registry credentials - exception: " + str(err))

        alltags = []
        for subscription_record in subscription_records:
            if not subscription_record['active']:
                continue

            try:
                fulltag = subscription_record['subscription_key']
                if fulltag not in alltags:
                    alltags.append(fulltag)

            except Exception as err:
                logger.warn("problem creating taglist for image watcher - exception: " + str(err))

        for registry_record in registry_creds:
            try:
                registry_status = anchore_engine.auth.docker_registry.ping_docker_registry(registry_record)
            except Exception as err:
                registry_record['record_state_key'] = 'auth_failure'
                registry_record['record_state_val'] = str(int(time.time()))
                logger.warn("registry ping failed - exception: " + str(err))

        logger.debug("checking tags for update: " + str(userId) + " : " + str(alltags))
        for fulltag in alltags:
            try:
                logger.debug("checking image latest info from registry: " + fulltag)

                image_info = anchore_engine.services.common.get_image_info(userId, "docker", fulltag, registry_lookup=True, registry_creds=registry_creds)
                logger.spew("checking image: got registry info: " + str(image_info))

                manifest = None
                try:
                    if 'manifest' in image_info:
                        manifest = json.dumps(image_info['manifest'])
                    else:
                        raise Exception("no manifest from get_image_info")
                except Exception as err:
                    manifest=None
                    raise Exception("could not fetch/parse manifest - exception: " + str(err))

                try:
                    dbfilter = {
                        'registry': image_info['registry'],
                        'repo': image_info['repo'],
                        'tag': image_info['tag'],
                        'digest': image_info['digest']
                    }
                except Exception as err:
                    raise Exception("could not prepare db filter for complete lookup check - exception: " + str(err))

                try:
                    stored_manifest = json.loads(archive.get_document(userId, 'manifest_data', image_info['digest']))
                    if not stored_manifest:
                        raise Exception("stored manifest is empty")
                except Exception as err:
                    logger.debug("found empty/invalid stored manifest, storing new: " + str(err))
                    rc = archive.put_document(userId, 'manifest_data', image_info['digest'], manifest)

                logger.debug("checking image: looking up image in db using dbfilter: " + str(dbfilter))
                with db.session_scope() as dbsession:
                    record = db_catalog_image.get_byimagefilter(userId, 'docker', dbfilter, session=dbsession)
                if record:
                    logger.debug("checking image: found match, no update, nothing to do: " + str(fulltag))
                else:
                    logger.info("checking image: found latest digest for tag is not in DB: should update and queue for analysis: tag="+str(fulltag) + " latest_digest="+str(dbfilter['digest']))
                    # get the set of existing digests
                    try:
                        last_dbfilter = {}
                        last_dbfilter.update(dbfilter)
                        last_dbfilter.pop('digest', None)

                        last_digests = []
                        last_annotations = {}
                        is_latest = True
                        with db.session_scope() as dbsession:
                            last_image_records = db_catalog_image.get_byimagefilter(userId, 'docker', last_dbfilter, session=dbsession)

                        if last_image_records:
                            for last_image_record in last_image_records:
                                imageDigest = last_image_record['imageDigest']
                                for image_detail in last_image_record['image_detail']:
                                    last_digests.append(image_detail['digest'])

                                # only do this (bring forward annotations) for the first found digest (last digest associated with tag)
                                if is_latest:
                                    if not last_annotations and last_image_record['annotations']:
                                        try:
                                            if last_image_record.get('annotations', '{}'):
                                                last_annotations.update(json.loads(last_image_record.get('annotations', '{}')))
                                        except:
                                            pass
                                    is_latest = False

                    except Exception as err:
                        logger.error(str(err))

                    # add and store the new image
                    with db.session_scope() as dbsession:
                        logger.debug("adding new image from tag watcher " + str(image_info))
                        image_records = catalog_impl.add_or_update_image(dbsession, userId, image_info['imageId'], tags=[image_info['fulltag']], digests=[image_info['fulldigest']], manifest=manifest, annotations=last_annotations)

                    if image_records:
                        image_record = image_records[0]
                    else:
                        image_record = {}

                    logger.info("checking image: added new image: " + str(image_record))
                    new_digests = [image_info['digest']]

                    # construct the notification and queue
                    try:
                        npayload = {
                            'last_eval': last_digests,
                            'curr_eval': new_digests,
                        }
                        if last_annotations:
                            npayload['annotations'] = last_annotations

                        rc = notifications.queue_notification(userId, fulltag, 'tag_update', npayload)
                        logger.debug("queued image tag update notification: " + fulltag)

                        #inobj = {
                        #    'userId': userId,
                        #    'subscription_key':fulltag,
                        #    'notificationId': str(uuid.uuid4()),
                        #    'last_eval':last_digests,
                        #    'curr_eval':new_digests,
                        #}
                        #if not simplequeue.is_inqueue(system_user_auth, 'tag_update', inobj):
                        #    qobj = simplequeue.enqueue(system_user_auth, 'tag_update', inobj)
                        #    logger.debug("queued image tag update notification: " + fulltag)

                    except Exception as err:
                        logger.error("failed to queue tag update notification - exception: " +str(err))
                        raise err

            except Exception as err:
                logger.error("failed to check/update image - exception: " + str(err))

    logger.debug("FIRING DONE: " + str(watcher))
    try:
        kwargs['mythread']['last_return'] = handler_success
    except:
        pass

    if anchore_engine.subsys.metrics.is_enabled() and handler_success:
        anchore_engine.subsys.metrics.summary_observe('anchore_monitor_runtime_seconds', time.time() - timer, function=watcher, status="success")
    else:
        anchore_engine.subsys.metrics.summary_observe('anchore_monitor_runtime_seconds', time.time() - timer, function=watcher, status="fail")

    return(True)

def check_feedmeta_update(dbsession):
    global feed_sync_updated
    return(feed_sync_updated)

def check_policybundle_update(userId, dbsession):
    global bundle_user_last_updated

    is_updated = True

    try:
        last_bundle_update = 0
        active_policy_record = db_policybundle.get_active_policy(userId, session=dbsession)
        if active_policy_record:
            last_bundle_update = active_policy_record['last_updated']
        else:
            logger.warn("user has no active policy - queueing just in case" + str(userId))
            return(is_updated)

        if userId not in bundle_user_last_updated:
            bundle_user_last_updated[userId] = last_bundle_update

        if last_bundle_update == bundle_user_last_updated[userId]:
            logger.debug("no bundle update detected since last cycle")
            is_updated = False
        else:
            logger.debug("bundle update detected since last cycle")
            bundle_user_last_updated[userId] = last_bundle_update
            is_updated = True
    except Exception as err:
        logger.warn("failed to get/parse active policy bundle for user ("+str(userId)+") - exception: " + str(err))
        bundle_user_last_updated[userId] = 0
        is_updated = True

    return(is_updated)

def handle_policyeval(*args, **kwargs):
    global system_user_auth, bundle_user_is_updated, feed_sync_updated

    watcher = str(kwargs['mythread']['taskType'])
    handler_success = True
    
    timer = time.time()
    logger.debug("FIRING: " + str(watcher))

    try:
        all_ready = anchore_engine.clients.common.check_services_ready(['policy_engine', 'simplequeue'])
        if not all_ready:
            logger.debug("FIRING DONE: policy eval (skipping due to required services not being available)")
            try:
                kwargs['mythread']['last_return'] = False
            except:
                pass
            return(True)

        with db.session_scope() as dbsession:
            feed_updated = check_feedmeta_update(dbsession)
            users = db_users.get_all(session=dbsession)

        for user in users:
            userId = user['userId']
            if userId == 'anchore-system':
                continue

            # policy evaluations

            doperform = False
            policy_sub_tags = []
            for subscription_type in ['policy_eval']:
                dbfilter = {'subscription_type': subscription_type}
                with db.session_scope() as dbsession:
                    subscription_records = db_subscriptions.get_byfilter(userId, session=dbsession, **dbfilter)
                for subscription_record in subscription_records:
                    if subscription_record['active']:
                        image_info = anchore_engine.services.common.get_image_info(userId, "docker", subscription_record['subscription_key'], registry_lookup=False, registry_creds=(None, None))
                        dbfilter = {'registry': image_info['registry'], 'repo': image_info['repo'], 'tag': image_info['tag']}
                        if dbfilter not in policy_sub_tags:
                            policy_sub_tags.append(dbfilter)

            for dbfilter in policy_sub_tags:
                with db.session_scope() as dbsession:
                    image_records = db_catalog_image.get_byimagefilter(userId, 'docker', dbfilter=dbfilter, onlylatest=True, session=dbsession)
                for image_record in image_records:
                    if image_record['analysis_status'] == taskstate.complete_state('analyze'):
                        imageDigest = image_record['imageDigest']
                        fulltag = dbfilter['registry'] + "/" + dbfilter['repo'] + ":" + dbfilter['tag']

                        # TODO - checks to avoid performing eval if nothing has changed
                        doperform = True
                        if doperform:
                            logger.debug("calling policy eval perform: " + str(fulltag) + " : " + str(imageDigest))
                            with db.session_scope() as dbsession:
                                try:
                                    rc = catalog_impl.perform_policy_evaluation(userId, imageDigest, dbsession, evaltag=fulltag)
                                except Exception as err:
                                    logger.warn("policy evaluation failed - exception: " + str(err))

    except Exception as err:
        logger.warn("failure in policy eval / vuln scan handler - exception: " + str(err))

    logger.debug("FIRING DONE: " + str(watcher))
    try:
        kwargs['mythread']['last_return'] = handler_success
    except:
        pass

    if anchore_engine.subsys.metrics.is_enabled() and handler_success:
        anchore_engine.subsys.metrics.summary_observe('anchore_monitor_runtime_seconds', time.time() - timer, function=watcher, status="success")
    else:
        anchore_engine.subsys.metrics.summary_observe('anchore_monitor_runtime_seconds', time.time() - timer, function=watcher, status="fail")

    return(True)    

def handle_analyzer_queue(*args, **kwargs):
    global system_user_auth

    watcher = str(kwargs['mythread']['taskType'])
    handler_success = True
    
    timer = time.time()
    logger.debug("FIRING: " + str(watcher))

    localconfig = anchore_engine.configuration.localconfig.get_config()
    try:
        max_working_time = int(localconfig['image_analyze_timeout_seconds'])
    except:
        max_working_time = 36000

    all_ready = anchore_engine.clients.common.check_services_ready(['policy_engine', 'simplequeue'])
    if not all_ready:
        logger.debug("FIRING DONE: analyzer queuer (skipping due to required services not being available)")
        try:
            kwargs['mythread']['last_return'] = False
        except:
            pass
        return(True)

    with db.session_scope() as dbsession:
        users = db_users.get_all(session=dbsession)

    for user in users:
        userId = user['userId']
        if userId == 'anchore-system':
            continue
            
        # do this in passes, for each analysis_status state

        with db.session_scope() as dbsession:
            dbfilter = {'analysis_status': taskstate.working_state('analyze')}
            workingstate_image_records = db_catalog_image.get_byfilter(userId, session=dbsession, **dbfilter)

        # first, evaluate images looking for those that have been in working state for too long and reset
        for image_record in workingstate_image_records:
            imageDigest = image_record['imageDigest']
            if image_record['image_status'] == taskstate.complete_state('image_status'):
                state_time = int(time.time()) - image_record['last_updated']
                logger.debug("image in working state for ("+str(state_time)+")s - "+str(imageDigest))
                if state_time > max_working_time:
                    logger.warn("image has been in working state ("+str(taskstate.working_state('analyze'))+") for over ("+str(max_working_time)+") seconds - resetting and requeueing for analysis")
                    image_record['analysis_status'] = taskstate.reset_state('analyze')
                    with db.session_scope() as dbsession:
                        db_catalog_image.update_record(image_record, session=dbsession)

        # next, look for any image in base state (not_analyzed) for queuing
        with db.session_scope() as dbsession:
            dbfilter = {'analysis_status': taskstate.base_state('analyze')}
            #dbfilter = {}
            basestate_image_records = db_catalog_image.get_byfilter(userId, session=dbsession, **dbfilter)

        for basestate_image_record in basestate_image_records:
            imageDigest = basestate_image_record['imageDigest']

            image_record = basestate_image_record
            #dbfilter = {'imageDigest': imageDigest}
            #with db.session_scope() as dbsession:
            #    image_records = db.db_catalog_image.get_byfilter(userId, session=dbsession, **dbfilter)
            #    image_record = image_records[0]

            if image_record['image_status'] == taskstate.complete_state('image_status'):
                logger.debug("image check")
                if image_record['analysis_status'] == taskstate.base_state('analyze'):
                    logger.debug("image in base state - "+str(imageDigest))
                    try:
                        manifest = archive.get_document(userId, 'manifest_data', image_record['imageDigest'])
                    except Exception as err:
                        manifest = {}

                    qobj = {}
                    qobj['userId'] = userId
                    qobj['imageDigest'] = image_record['imageDigest']
                    qobj['manifest'] = manifest
                    try:
                        if not simplequeue.is_inqueue(system_user_auth, 'images_to_analyze', qobj):
                            # queue image for analysis
                            logger.debug("queued image for analysis: " + str(imageDigest))
                            qobj = simplequeue.enqueue(system_user_auth, 'images_to_analyze', qobj)

                            # set the appropriate analysis state for image 
                            #image_record['analysis_status'] = taskstate.queued_state('analyze')
                            #image_record['analysis_status'] = taskstate.working_state('analyze')
                            #with db.session_scope() as dbsession:
                            #    rc = db.db_catalog_image.update_record(image_record, session=dbsession)

                        else:
                            logger.debug("image already queued")
                    except Exception as err:
                        logger.error("failed to check/queue image for analysis - exception: " + str(err))
                
    logger.debug("FIRING DONE: " + str(watcher))
    try:
        kwargs['mythread']['last_return'] = handler_success
    except:
        pass

    if anchore_engine.subsys.metrics.is_enabled() and handler_success:
        anchore_engine.subsys.metrics.summary_observe('anchore_monitor_runtime_seconds', time.time() - timer, function=watcher, status="success")
    else:
        anchore_engine.subsys.metrics.summary_observe('anchore_monitor_runtime_seconds', time.time() - timer, function=watcher, status="fail")

    return(True)

def sync_policy_bundle(user_auth, anchoreio_user, anchoreio_pw, localconfig):
    url = localconfig.get('policy_sync_url', None)
    if not url:
        raise Exception("cannot read policy_sync_url from loaded configuration")

    client = anchore_engine.auth.anchore_io.get_anchoreio_client(anchoreio_user, anchoreio_pw)

    bundledata = None
    r = client.authenticated_get(url)
    if r.get('status_code') == 200 and r.get('success'):
        bundledataraw = r.get('text')
        if bundledataraw:
            bundledata = json.loads(bundledataraw).get('bundle')
    else:
        raise Exception("httpcode={} success={} text={}".format(r.get('status_code'), r.get('success', False), r.get('text', "")))

    # schema check
    try:
        p_client = policy_engine.get_client(user=user_auth[0], password=user_auth[1])
        response = p_client.validate_bundle(policy_bundle=bundledata)
        if not response.valid:
            raise Exception("validation failed")
    except ApiException as err:
        raise Exception('Error response from policy service during bundle validation. Validation could not be performed: {}'.format(err))

    return(bundledata)

def handle_policy_bundle_sync(*args, **kwargs):
    global system_user_auth, bundle_user_last_updated

    watcher = str(kwargs['mythread']['taskType'])
    handler_success = True
    
    timer = time.time()
    logger.debug("FIRING: " + str(watcher))

    all_ready = anchore_engine.clients.common.check_services_ready(['policy_engine'])
    count=0
    retries=10
    while (not all_ready) and (count < retries):
        logger.info("policy engine is not yet ready - retrying {}/{}".format(count, retries))
        time.sleep(1)
        count = count + 1
        all_ready = anchore_engine.clients.common.check_services_ready(['policy_engine'])

    if not all_ready:
        logger.warn("policy engine not ready after {} retries - will try again next cycle".format(retries))
        logger.debug("FIRING DONE: " + str(watcher))
        return(True)

    localconfig = anchore_engine.configuration.localconfig.get_config()

    with db.session_scope() as dbsession:
        users = db_users.get_all(session=dbsession)
        for user in users:
            userId = user['userId']
            if userId == 'anchore-system':
                continue

            try:
                autosync = False
                try:
                    autosync = localconfig['credentials']['users'][userId]['auto_policy_sync']
                except:
                    pass

                if not autosync:
                    logger.debug("user ("+str(userId)+") has auto_policy_sync set to false in config - skipping bundle sync")
                    continue
                else:
                    logger.debug("user ("+str(userId)+") has auto_policy_sync set to true in config - attempting bundle sync")

                anchorecredstr = localconfig['credentials']['users'][userId]['external_service_auths']['anchoreio']['anchorecli']['auth']
                anchore_user, anchore_pw = anchorecredstr.split(':')

                try:
                    anchore_user_bundle = sync_policy_bundle((user['userId'], user['password']), anchore_user, anchore_pw, localconfig)
                except Exception as err:
                    raise Exception("anchore.io bundle sync failed - exception: " + str(err))
                    
                #with localanchore.get_anchorelock():
                #    anchore_user_bundle = localanchore.get_bundle(anchore_user, anchore_pw)
                #try:
                #    import anchore.anchore_policy
                #    rc = anchore.anchore_policy.verify_policy_bundle(bundle=anchore_user_bundle)
                #    if not rc:
                #        raise Exception("input bundle does not conform to anchore bundle schema")
                #except Exception as err:
                #    raise Exception("cannot run bundle schema verification - exception: " + str(err))

                # TODO should compare here to determine if new bundle is different from stored/active bundle
                do_update = True
                try:
                    current_policy_record = db_policybundle.get_active_policy(userId, session=dbsession)
                    if current_policy_record:
                        current_policy_bundle = archive.get_document(userId, 'policy_bundles', current_policy_record['policyId'])
                        if current_policy_bundle and current_policy_bundle == anchore_user_bundle:
                            logger.debug("synced bundle is the same as currently installed/active bundle")
                            do_update = False

                            # special case for upgrade when adding the policy_source column
                            try:
                                if current_policy_record['policy_source'] == 'local':
                                    logger.debug("upgrade case detected - need to write policy_source as anchoreio for existing policy bundle")
                                    do_update = True
                            except:
                                pass

                        else:
                            logger.debug("synced bundle is different from currently installed/active bundle")
                            do_update = True
                except Exception as err:
                    logger.warn("unable to compare synced bundle with current bundle: " + str(err))


                if do_update:

                    logger.spew("synced bundle object: " + json.dumps(anchore_user_bundle, indent=4))
                    new_policybundle_record = anchore_engine.services.common.make_policy_record(userId, anchore_user_bundle, policy_source="anchore.io", active=True)
                    logger.spew("created new bundle record: " + json.dumps(new_policybundle_record, indent=4))

                    policyId = new_policybundle_record['policyId']
                    rc = archive.put_document(userId, 'policy_bundles', policyId, anchore_user_bundle)
                    logger.debug("bundle record archived: " + str(userId) + " : " + str(policyId))
                    rc = db_policybundle.update(policyId, userId, True, new_policybundle_record, session=dbsession)
                    logger.debug("bundle record stored: " + str(userId) + " : " + str(policyId))
                    if not rc:
                        raise Exception("DB update failed")
                    else:
                        rc = db_policybundle.set_active_policy(policyId, userId, session=dbsession)
                        bundle_user_last_updated[userId] = 0
            except Exception as err:
                logger.warn("no valid bundle available for user ("+str(userId)+") - exception: " + str(err))

    logger.debug("FIRING DONE: " + str(watcher))
    try:
        kwargs['mythread']['last_return'] = handler_success
    except:
        pass

    if anchore_engine.subsys.metrics.is_enabled() and handler_success:
        anchore_engine.subsys.metrics.summary_observe('anchore_monitor_runtime_seconds', time.time() - timer, function=watcher, status="success")
    else:
        anchore_engine.subsys.metrics.summary_observe('anchore_monitor_runtime_seconds', time.time() - timer, function=watcher, status="fail")

    return(True)

def handle_notifications(*args, **kwargs):
    global system_user_auth

    watcher = str(kwargs['mythread']['taskType'])
    handler_success = True
    
    timer = time.time()
    logger.debug("FIRING: " + str(watcher))

    with db.session_scope() as dbsession:
        # special handling of the error event queue, if configured as a webhook
        try:
            localconfig = anchore_engine.configuration.localconfig.get_config()
            try:
                notification_timeout = int(localconfig['webhooks']['notification_retry_timeout'])
            except:
                notification_timeout = 30

            logger.debug("notification timeout: " + str(notification_timeout))

            do_erreventhooks = False
            try:
                if localconfig['webhooks']['error_event']:
                    do_erreventhooks = True
            except:
                logger.debug("error_event webhook is not configured, skipping webhook for error_event")

            if do_erreventhooks:
                system_user_record = db_users.get('admin', session=dbsession)
                errevent_records = db_eventlog.get_all(session=dbsession)
                for errevent in errevent_records:
                    notification = errevent
                    userId = system_user_record['userId']
                    notificationId = str(uuid.uuid4())
                    subscription_type = 'error_event'
                    notification_record = notifications.make_notification(system_user_record, 'error_event', notification)
                    logger.spew("Storing NOTIFICATION: " + str(system_user_record) + str(notification_record))
                    db_queues.add(subscription_type, userId, notificationId, notification_record, 0, int(time.time() + notification_timeout), session=dbsession)
                    db_eventlog.delete_record(errevent, session=dbsession)
        except Exception as err:
            logger.warn("failed to queue error eventlog for notification - exception: " + str(err))

        # regular event queue notifications
        for subscription_type in anchore_engine.services.common.subscription_types + ['error_event']:
            logger.debug("notifier: " + subscription_type)
            users = db_users.get_all(session=dbsession)

            try:
                qlen = simplequeue.qlen(system_user_auth, subscription_type)
            except Exception as err:
                logger.debug("problem looking for notifications in queue: " + str(subscription_type) + " - exception: " + str(err))
                qlen = 0

            while(qlen > 0):
                pupdate_record = simplequeue.dequeue(system_user_auth, subscription_type)
                if pupdate_record:
                    logger.debug("got notification from queue: " + json.dumps(pupdate_record, indent=4))
                    notification = pupdate_record['data']
                    userId = notification['userId']
                    subscription_key = notification['subscription_key']
                    notificationId = notification['notificationId']
                    for user in users:
                        try:
                            if userId == user['userId']:
                                dbfilter = {'subscription_type': subscription_type, 'subscription_key': subscription_key}
                                subscription_records = db_subscriptions.get_byfilter(user['userId'], session=dbsession, **dbfilter)
                                if subscription_records:
                                    subscription = subscription_records[0]
                                    if subscription and subscription['active']:
                                        notification_record = notifications.make_notification(user, subscription_type, notification)
                                        logger.spew("Storing NOTIFICATION: " + str(user) + str(notification_record))
                                        db_queues.add(subscription_type, userId, notificationId, notification_record, 0, int(time.time() + notification_timeout), session=dbsession)

                        except Exception as err:
                            import traceback
                            traceback.print_exc()
                            logger.warn("cannot store notification to DB - exception: " + str(err))

                qlen = simplequeue.qlen(system_user_auth, subscription_type)

            for user in users:
                notification_records = db_queues.get_all(subscription_type, user['userId'], session=dbsession)
                for notification_record in notification_records:
                    logger.debug("drained to send: " + json.dumps(notification_record))
                    try:
                        rc = notifications.notify(user, notification_record)
                        if rc:
                            db_queues.delete_record(notification_record, session=dbsession)
                    except Exception as err:
                        logger.debug("failed to send notification, storing for retry - exception: " + str(err))
                        notification_record['tries'] = int(time.time())
                        if notification_record['tries'] > notification_record['max_tries']:
                            logger.error("hit max notification timeout: dropping notificaion")
                            db_queues.delete_record(notification_record, session=dbsession)
                        else:
                            db_queues.update_record(notification_record, session=dbsession)

    logger.debug("FIRING DONE: " + str(watcher))
    try:
        kwargs['mythread']['last_return'] = handler_success
    except:
        pass

    if anchore_engine.subsys.metrics.is_enabled() and handler_success:
        anchore_engine.subsys.metrics.summary_observe('anchore_monitor_runtime_seconds', time.time() - timer, function=watcher, status="success")
    else:
        anchore_engine.subsys.metrics.summary_observe('anchore_monitor_runtime_seconds', time.time() - timer, function=watcher, status="fail")

    return(True)

def handle_metrics(*args, **kwargs):
    cycle_timer = kwargs['mythread']['cycle_timer']

    while(True):

        # perform some DB read/writes for metrics gathering
        if anchore_engine.subsys.metrics.is_enabled():

            # DB probes
            anchore_record = None
            try:
                with anchore_engine.subsys.metrics.get_summary_obj("anchore_db_read_seconds").time() as mtimer:
                    with db.session_scope() as dbsession:
                        anchore_record = db_anchore.get(session=dbsession)
            except Exception as err:
                logger.warn("unable to perform DB read probe - exception: " + str(err))

            if anchore_record:
                try:
                    with anchore_engine.subsys.metrics.get_summary_obj("anchore_db_write_seconds").time() as mtimer:
                        with db.session_scope() as dbsession:
                            anchore_record['record_state_val'] = str(time.time())
                            rc = db_anchore.update_record(anchore_record, session=dbsession)

                except Exception as err:
                    logger.warn("unable to perform DB write probe - exception: " + str(err))

            try:
                with anchore_engine.subsys.metrics.get_summary_obj("anchore_db_readwrite_seconds").time() as mtimer:
                    with db.session_scope() as dbsession:
                        anchore_record = db_anchore.get(session=dbsession)
                        anchore_record['record_state_val'] = str(time.time())
                        rc = db_anchore.update_record(anchore_record, session=dbsession)
            except Exception as err:
                logger.warn("unable to perform DB read/write probe - exception: " + str(err))


            # FS probes
            localconfig = anchore_engine.configuration.localconfig.get_config()
            try:
                tmpdir = localconfig['tmp_dir']
                svfs = os.statvfs(tmpdir)
                available_bytes = svfs.f_bsize * svfs.f_bavail
                anchore_engine.subsys.metrics.gauge_set("anchore_tmpspace_available_bytes", available_bytes)
            except Exception as err:
                logger.warn("unable to detect available bytes probe - exception: " + str(err))

        time.sleep(cycle_timer)

click = 0
running = False
last_run = 0
system_user_auth = ('anchore-system', '')
# policy update check data
feed_sync_updated = False
bundle_user_last_updated = {}
bundle_user_is_updated = {}

watchers = {
    'image_watcher': {'handler': handle_image_watcher, 'task_lease_id': 'image_watcher', 'taskType': 'handle_image_watcher', 'args': [], 'cycle_timer': 600, 'min_cycle_timer': 300, 'max_cycle_timer': 86400*7, 'last_queued': 0, 'last_return': False, 'initialized': False},
    'repo_watcher': {'handler': handle_repo_watcher, 'task_lease_id': 'repo_watcher', 'taskType': 'handle_repo_watcher', 'args': [], 'cycle_timer': 60, 'min_cycle_timer': 60, 'max_cycle_timer': 86400*7, 'last_queued': 0, 'last_return': False, 'initialized': False},
    'policy_eval': {'handler':handle_policyeval, 'task_lease_id': 'policy_eval', 'taskType': 'handle_policyeval', 'args': [], 'cycle_timer': 300, 'min_cycle_timer': 60, 'max_cycle_timer': 86400*2, 'last_queued': 0, 'last_return': False, 'initialized': False},
    'policy_bundle_sync': {'handler':handle_policy_bundle_sync, 'task_lease_id': 'policy_bundle_sync','taskType': 'handle_policy_bundle_sync', 'args': [], 'cycle_timer': 3600, 'min_cycle_timer': 300, 'max_cycle_timer': 86400*2, 'last_queued': 0, 'last_return': False, 'initialized': False},
    'analyzer_queue': {'handler':handle_analyzer_queue, 'task_lease_id': 'analyzer_queue','taskType': 'handle_analyzer_queue', 'args': [], 'cycle_timer': 5, 'min_cycle_timer': 1, 'max_cycle_timer': 7200, 'last_queued': 0, 'last_return': False, 'initialized': False},
    'notifications': {'handler':handle_notifications, 'task_lease_id': 'notifications','taskType': 'handle_notifications', 'args': [], 'cycle_timer': 10, 'min_cycle_timer': 10, 'max_cycle_timer': 86400*2, 'last_queued': 0, 'last_return': False, 'initialized': False},
    'vulnerability_scan': {'handler':handle_vulnerability_scan, 'task_lease_id': 'vulnerability_scan', 'taskType': 'handle_vulnerability_scan', 'args': [], 'cycle_timer': 300, 'min_cycle_timer': 60, 'max_cycle_timer': 86400*2, 'last_queued': 0, 'last_return': False, 'initialized': False},
    'service_watcher': {'handler':handle_service_watcher, 'task_lease_id': False, 'taskType': None, 'args': [], 'cycle_timer': 10, 'min_cycle_timer': 1, 'max_cycle_timer': 300, 'last_queued': 0, 'last_return': False, 'initialized': False},
    'service_heartbeat': {'handler': anchore_engine.subsys.servicestatus.handle_service_heartbeat, 'task_lease_id': False, 'taskType': None, 'args': [servicename], 'cycle_timer': 60, 'min_cycle_timer': 60, 'max_cycle_timer': 60, 'last_queued': 0, 'last_return': False, 'initialized': False},
    'handle_metrics': {'handler': handle_metrics, 'task_lease_id': False, 'taskType': None, 'args': [], 'cycle_timer': 60, 'min_cycle_timer': 60, 'max_cycle_timer': 60, 'last_queued': 0, 'last_return': False, 'initialized': False},
}

watcher_task_template = {
    'taskType': None,
    'watcher': None,
}
watcher_threads = {}

default_lease_ttl = 60 # 1 hour ttl, should be more than enough in most cases


def watcher_func(*args, **kwargs):
    global system_user_auth

    while(True):
        logger.debug("starting generic watcher")
        all_ready = anchore_engine.clients.common.check_services_ready(['simplequeue'])
        if not all_ready:
            logger.info("simplequeue service not yet ready, will retry")
        else:
            try:
                logger.debug("attempting dequeue")
                qobj = simplequeue.dequeue(system_user_auth, 'watcher_tasks', max_wait_seconds=30)
                logger.debug("dequeue complete")

                if qobj:
                    logger.debug("got task from queue: " + str(qobj))
                    watcher = qobj['data']['watcher']
                    handler = watchers[watcher]['handler']
                    args = []
                    kwargs = {'mythread': watchers[watcher]}

                    lease_id = watchers[watcher]['task_lease_id']

                    # Old way
                    timer = time.time()
                    if not lease_id:
                        logger.debug('No task lease defined for watcher {}, initiating without lock protection'.format(watcher))
                        rc = handler(*args, **kwargs)
                    else:
                        rc = simplequeue.run_target_with_lease(system_user_auth, lease_id, handler, ttl=default_lease_ttl, *args, **kwargs)

                else:
                    logger.debug("nothing in queue")
            except Exception as err:
                logger.warn("failed to process task this cycle: " + str(err))
        logger.debug("generic watcher done")
        time.sleep(5)

def schedule_watcher(watcher):
    global watchers, watcher_task_template, system_user_auth

    if watcher not in watchers:
        logger.warn("input watcher {} not in list of available watchers {}".format(watcher, watchers.keys()))
        return(False)

    if watchers[watcher]['taskType']:
        logger.debug("should queue job: " + watcher)
        watcher_task = copy.deepcopy(watcher_task_template)
        watcher_task['watcher'] = watcher
        watcher_task['taskType'] = watchers[watcher]['taskType']
        try:
            if not simplequeue.is_inqueue(system_user_auth, 'watcher_tasks', watcher_task):
                qobj = simplequeue.enqueue(system_user_auth, 'watcher_tasks', watcher_task)
                logger.debug(str(watcher_task)+": init task queued: " + str(qobj))
            else:
                logger.debug(str(watcher_task)+": init task already queued")

            watchers[watcher]['last_queued'] = time.time()
        except Exception as err:
            logger.warn("failed to enqueue watcher task: " + str(err))    

    return(True)

def monitor_func(**kwargs):
    global click, running, last_queued, system_user_auth, watchers, last_run
    
    if click < 5:
        click = click + 1
        logger.debug("Catalog monitor starting in: " + str(5 - click))
        return(True)

    if running or ((time.time() - last_run) < kwargs['kick_timer']):
        return(True)

    logger.debug("FIRING: catalog_monitor")
    try:
        localconfig = anchore_engine.configuration.localconfig.get_config()
        system_user_auth = localconfig['system_user_auth']

        for watcher in watchers.keys():
            if not watchers[watcher]['initialized']:
                # first time
                if 'cycle_timers' in kwargs and watcher in kwargs['cycle_timers']:
                    try:
                        the_cycle_timer = watchers[watcher]['cycle_timer']
                        min_cycle_timer = watchers[watcher]['min_cycle_timer']
                        max_cycle_timer = watchers[watcher]['max_cycle_timer']

                        config_cycle_timer = int(kwargs['cycle_timers'][watcher])
                        if config_cycle_timer < 0:
                            the_cycle_timer = abs(int(config_cycle_timer))
                        elif config_cycle_timer < min_cycle_timer:
                            logger.warn("configured cycle timer for handler ("+str(watcher)+") is less than the allowed min ("+str(min_cycle_timer)+") - using allowed min")
                            the_cycle_timer = min_cycle_timer
                        elif config_cycle_timer > max_cycle_timer:
                            logger.warn("configured cycle timer for handler ("+str(watcher)+") is greater than the allowed max ("+str(max_cycle_timer)+") - using allowed max")
                            the_cycle_timer = max_cycle_timer
                        else:
                            the_cycle_timer = config_cycle_timer

                        watchers[watcher]['cycle_timer'] = the_cycle_timer
                    except Exception as err:
                        logger.warn("exception setting custom cycle timer for handler ("+str(watcher)+") - using default")

                watchers[watcher]['initialized'] = True

            if watcher not in watcher_threads:
                if watchers[watcher]['taskType']:
                    # spin up a generic task watcher
                    logger.debug("starting generic task thread")
                    watcher_threads[watcher] = threading.Thread(target=watcher_func, args=[watcher], kwargs={})
                    watcher_threads[watcher].start()
                else:
                    # spin up a specific looping watcher thread
                    watcher_threads[watcher] = threading.Thread(target=watchers[watcher]['handler'], args=watchers[watcher]['args'], kwargs={'mythread': watchers[watcher]})
                    watcher_threads[watcher].start()

            all_ready = anchore_engine.clients.common.check_services_ready(['simplequeue'])
            if not all_ready:
                logger.info("simplequeue service not yet ready, will retry")
            elif time.time() - watchers[watcher]['last_queued'] > watchers[watcher]['cycle_timer']:
                rc = schedule_watcher(watcher)

                #if watchers[watcher]['taskType']:
                #    logger.debug("should queue job: " + watcher)
                #    watcher_task = copy.deepcopy(watcher_task_template)
                #    watcher_task['watcher'] = watcher
                #    watcher_task['taskType'] = watchers[watcher]['taskType']
                #    try:
                #        if not simplequeue.is_inqueue(system_user_auth, 'watcher_tasks', watcher_task):
                #            qobj = simplequeue.enqueue(system_user_auth, 'watcher_tasks', watcher_task)
                #            logger.debug(str(watcher_task)+": init task queued: " + str(qobj))
                #        else:
                #            logger.debug(str(watcher_task)+": init task already queued")
                #        watchers[watcher]['last_queued'] = time.time()
                #    except Exception as err:
                #        logger.warn("failed to enqueue watcher task: " + str(err))

    except Exception as err:
        logger.error(str(err))
    finally:
        logger.debug("FIRING DONE: catalog_monitor")
        running = False
        last_run = time.time()

    logger.debug("exiting monitor thread")

monitor_thread = None
def monitor(**kwargs):
    global monitor_thread
    try:
        donew = False
        if monitor_thread:
            if monitor_thread.isAlive():
                logger.spew("MON: thread still running")
            else:
                logger.spew("MON: thread stopped running")
                donew = True
                monitor_thread.join()
                logger.spew("MON: thread joined: isAlive=" + str(monitor_thread.isAlive()))
        else:
            logger.spew("MON: no thread")
            donew = True

        if donew:
            logger.spew("MON: starting")
            monitor_thread = threading.Thread(target=monitor_func, kwargs=kwargs)
            monitor_thread.start()
        else:
            logger.spew("MON: skipping")

    except Exception as err:
        logger.warn("MON thread start exception: " + str(err))


