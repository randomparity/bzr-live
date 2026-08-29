FROM ubuntu:24.04@sha256:33ceb71981b602c1a7443a53469e4dba065f7503eab3078a2d7a57a2ab987517

ARG DEBIAN_FRONTEND=noninteractive
ARG BZ_SOURCE_SHA=644c66f45ce0b1b2746a31a061fbd96886278225
ARG BZ_SOURCE_SHA256=bf4f79d9e6b230ad01d9b3c4d12a56af7eebd96cc76d5c9231a696a0750e1660

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      apache2 \
      build-essential \
      ca-certificates \
      curl \
      graphviz \
      libapache2-mod-perl2 \
      libapache2-mod-perl2-dev \
      libappconfig-perl \
      libauthen-radius-perl \
      libauthen-sasl-perl \
      libcache-memcached-perl \
      libcgi-pm-perl \
      libchart-perl \
      libdaemon-generic-perl \
      libdate-calc-perl \
      libdatetime-perl \
      libdatetime-timezone-perl \
      libdbi-perl \
      libdbix-connector-perl \
      libemail-address-perl \
      libemail-address-xs-perl \
      libemail-mime-modifier-perl \
      libemail-mime-perl \
      libemail-reply-perl \
      libemail-sender-perl \
      libencode-detect-perl \
      libfile-copy-recursive-perl \
      libfile-mimeinfo-perl \
      libfile-slurp-perl \
      libfile-which-perl \
      libgd-dev \
      libgd-graph-perl \
      libhtml-formattext-withlinks-perl \
      libhtml-scrubber-perl \
      libjson-rpc-perl \
      libjson-xs-perl \
      liblocale-codes-perl \
      libmath-random-isaac-perl \
      libmath-random-isaac-xs-perl \
      libmodule-build-perl \
      libmysqlclient-dev \
      libnet-ldap-perl \
      libsoap-lite-perl \
      libtemplate-perl \
      libtemplate-plugin-gd-perl \
      libtest-taint-perl \
      libtheschwartz-perl \
      libxml-perl \
      libxml-twig-perl \
      mariadb-client \
      patchutils \
      perlmagick \
      tzdata \
 && cpan -T install Template::Toolkit DBD::MariaDB PatchReader \
 && apt-get purge -y --auto-remove build-essential \
 && rm -rf /root/.cpan /var/lib/apt/lists/*

WORKDIR /var/www/html
RUN curl --fail --location --silent --show-error \
      --output /tmp/bugzilla.tar.gz \
      "https://github.com/bugzilla/bugzilla/archive/${BZ_SOURCE_SHA}.tar.gz" \
 && printf '%s  %s\n' "$BZ_SOURCE_SHA256" /tmp/bugzilla.tar.gz | sha256sum --check --strict \
 && tar --extract --gzip --file /tmp/bugzilla.tar.gz \
      --directory /var/www/html --strip-components=1 \
 && rm /tmp/bugzilla.tar.gz \
 && rm -rf data localconfig index.html \
 && install -d -o www-data -g www-data /var/www/html/data

COPY containers/bugzilla/apache.conf /etc/apache2/sites-available/000-default.conf
COPY containers/bugzilla/checksetup_answers.txt /opt/bzr-live/checksetup_answers.txt
COPY --chmod=0755 containers/bugzilla/entrypoint.sh /usr/local/bin/bzr-live-bugzilla

RUN a2enmod expires headers rewrite \
 && a2dismod mpm_event \
 && a2enmod mpm_prefork

EXPOSE 80
ENTRYPOINT ["/usr/local/bin/bzr-live-bugzilla"]
