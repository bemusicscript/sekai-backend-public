ARG PHP_VERSION=latest
ARG APP_VERSION=2026.1

FROM trafex/php-nginx:${PHP_VERSION}
ENV APP_VERSION=$APP_VERSION
WORKDIR /var/www/html

# Install nodejs
RUN echo "?"
USER root
RUN apk add --no-cache \
    php85-pecl-redis \
    php85-pecl-zstd \
    nodejs \
    npm

# Install js confuser
RUN npm install -g js-confuser html-minifier clean-css --prefix /var/www/npm
RUN chown nobody:nogroup /var/log/php85

# Switch to use a non-root user from here on
USER nobody

# Expose the port nginx is reachable on
EXPOSE 8080

# Let supervisord start nginx & php-fpm
CMD ["/usr/bin/supervisord", "-c", "/etc/supervisor/conf.d/supervisord.conf"]
