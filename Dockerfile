FROM centos:7

ENV LANG=en_US.UTF-8
EXPOSE 8228 8338 8087 8082

RUN yum -y update && yum -y install epel-release && yum -y install skopeo python-pip dpkg gcc python-devel openssl-devel psmisc && yum clean all -y
RUN pip install --upgrade pip && pip install --upgrade setuptools && pip install anchorecli

COPY . /root/anchore-engine
RUN cd /root/anchore-engine/ && pip install --upgrade .

CMD /usr/bin/anchore-engine
