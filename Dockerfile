FROM debian:jessie

ENTRYPOINT [ "/marathon-lb/run" ]
CMD        [ "sse", "-m", "http://master.mesos:8080", "--health-check", "--group", "external" ]
EXPOSE     80 81 443 9090

COPY  . /marathon-lb
RUN sed -i $'s/$/\r/' /marathon-lb/cors.options.http

RUN apt-get update && apt-get install -y python3 python3-pip openssl libssl-dev runit \
    wget build-essential libpcre3 libpcre3-dev python3-dateutil socat iptables libreadline-dev \
    && pip3 install -r /marathon-lb/requirements.txt \
    && /marathon-lb/build-haproxy.sh \
    && apt-get remove -yf --auto-remove wget libssl-dev build-essential libpcre3-dev libreadline-dev \
    && rm -rf /var/lib/apt/lists/*

RUN apt-get update && apt-get install -y syslog-ng

WORKDIR /marathon-lb
