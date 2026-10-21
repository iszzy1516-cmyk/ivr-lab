FROM andrius/asterisk:20

# Core english sounds + music-on-hold. Debian ships them under /usr/share/asterisk;
# this Asterisk build reads /var/lib/asterisk, so link them in.
# Debian ships sounds under /usr/share/asterisk; this Asterisk build reads
# /var/lib/asterisk. The language dir name varies by Debian release
# (en, en_US, en_US_f_Allison), so glob it.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      asterisk-core-sounds-en-gsm \
      asterisk-moh-opsound-gsm \
 && rm -rf /var/lib/apt/lists/* \
 && mkdir -p /var/lib/asterisk/sounds /var/lib/asterisk/moh \
 && lang_src=$(ls -d /usr/share/asterisk/sounds/en* | head -1) \
 && cp -a "$lang_src" /var/lib/asterisk/sounds/en \
 && cp -a /usr/share/asterisk/moh/. /var/lib/asterisk/moh/ \
 && chown -R asterisk:asterisk /var/lib/asterisk

COPY cc-entrypoint.sh /usr/local/bin/cc-entrypoint.sh
RUN chmod +x /usr/local/bin/cc-entrypoint.sh

# Configs are bind-mounted from ./config at runtime (see docker-compose.yml).
CMD ["/bin/sh", "/usr/local/bin/cc-entrypoint.sh"]
