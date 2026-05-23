ARG CADDY_VERSION=2.11.3
FROM caddy:${CADDY_VERSION}-builder-alpine AS builder

RUN xcaddy build \
	--with github.com/mholt/caddy-l4 \
	--with github.com/caddyserver/transform-encoder \
	--with github.com/hslatman/caddy-crowdsec-bouncer/http@main \
	--with github.com/hslatman/caddy-crowdsec-bouncer/appsec@main \
	--with github.com/hslatman/caddy-crowdsec-bouncer/layer4@main

FROM caddy:${CADDY_VERSION} AS caddy
WORKDIR /
COPY --from=builder /usr/bin/caddy /usr/bin/caddy

# from official caddy docker image
ENV XDG_CONFIG_HOME=/config
ENV XDG_DATA_HOME=/data

RUN mkdir -p /config/caddy /data/caddy /etc/caddy /usr/share/caddy && \
    chmod 1777 /config/caddy /data/caddy;

CMD ["caddy", "run", "--config", "/etc/caddy/Caddyfile", "--adapter", "caddyfile"]
