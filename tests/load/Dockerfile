FROM ubuntu:24.04
RUN apt-get update \
 && apt-get install -y --no-install-recommends sip-tester \
 && rm -rf /var/lib/apt/lists/*
COPY uac.xml /scenarios/uac.xml
ENTRYPOINT ["sipp"]
