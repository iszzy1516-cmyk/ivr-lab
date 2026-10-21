#!/bin/sh
# Fill in the external IP so SIP/RTP packets carry a LAN-reachable address.
EXTERNAL_IP="${SIP_EXTERNAL_IP:-127.0.0.1}"
sed -i "s/__EXTERNAL_IP__/${EXTERNAL_IP}/g" /etc/asterisk/pjsip.conf

exec /usr/sbin/asterisk -vvvdddf -T -W -U asterisk -p
