FROM ubuntu:24.04@sha256:33ceb71981b602c1a7443a53469e4dba065f7503eab3078a2d7a57a2ab987517

ARG DEBIAN_FRONTEND=noninteractive
ARG BZ_SOURCE_SHA=644c66f45ce0b1b2746a31a061fbd96886278225
ARG BZ_SOURCE_SHA256=bf4f79d9e6b230ad01d9b3c4d12a56af7eebd96cc76d5c9231a696a0750e1660
ARG DBD_MARIADB_VERSION=1.24
ARG DBD_MARIADB_SHA256=f977a25b4116a0a95a7c8a894fd37097abe19af9a6a9ed4d800604ec17873fe4
ARG TEMPLATE_TOOLKIT_VERSION=3.106
ARG TEMPLATE_TOOLKIT_SHA256=c7474050be80201f1fb55f0a569b9c0ab6c1c3f0cebbd7e601bda9b4046eec85
ARG PATCHREADER_VERSION=0.9.6
ARG PATCHREADER_SHA256=b8de37460347bb5474dc01916ccb31dd2fe0cd92242c4a32d730e8eb087c323c

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
      libdevel-checklib-perl \
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
      libmariadb-dev \
      libmodule-build-perl \
      libnet-ldap-perl \
      libsoap-lite-perl \
      libtemplate-perl \
      libtemplate-plugin-gd-perl \
      libtest-deep-perl \
      libtest-taint-perl \
      libtheschwartz-perl \
      libxml-perl \
      libxml-twig-perl \
      libxmlrpc-lite-perl \
      mariadb-client \
      patchutils \
      perlmagick \
      tzdata \
 && curl --fail --location --silent --show-error \
      --output "/tmp/Template-Toolkit-${TEMPLATE_TOOLKIT_VERSION}.tar.gz" \
      "https://cpan.metacpan.org/authors/id/T/TO/TODDR/Template-Toolkit-${TEMPLATE_TOOLKIT_VERSION}.tar.gz" \
 && curl --fail --location --silent --show-error \
      --output "/tmp/DBD-MariaDB-${DBD_MARIADB_VERSION}.tar.gz" \
      "https://cpan.metacpan.org/authors/id/P/PA/PALI/DBD-MariaDB-${DBD_MARIADB_VERSION}.tar.gz" \
 && curl --fail --location --silent --show-error \
      --output "/tmp/PatchReader-${PATCHREADER_VERSION}.tar.gz" \
      "https://cpan.metacpan.org/authors/id/T/TM/TMANNERM/PatchReader-${PATCHREADER_VERSION}.tar.gz" \
 && printf '%s  %s\n' "$TEMPLATE_TOOLKIT_SHA256" "/tmp/Template-Toolkit-${TEMPLATE_TOOLKIT_VERSION}.tar.gz" \
      | sha256sum --check --strict \
 && printf '%s  %s\n' "$DBD_MARIADB_SHA256" "/tmp/DBD-MariaDB-${DBD_MARIADB_VERSION}.tar.gz" \
      | sha256sum --check --strict \
 && printf '%s  %s\n' "$PATCHREADER_SHA256" "/tmp/PatchReader-${PATCHREADER_VERSION}.tar.gz" \
      | sha256sum --check --strict \
 && tar --extract --gzip --file "/tmp/Template-Toolkit-${TEMPLATE_TOOLKIT_VERSION}.tar.gz" --directory /tmp \
 && tar --extract --gzip --file "/tmp/DBD-MariaDB-${DBD_MARIADB_VERSION}.tar.gz" --directory /tmp \
 && tar --extract --gzip --file "/tmp/PatchReader-${PATCHREADER_VERSION}.tar.gz" --directory /tmp \
 && cd "/tmp/Template-Toolkit-${TEMPLATE_TOOLKIT_VERSION}" \
 && perl Makefile.PL \
 && make \
 && make test \
 && make install \
 && cd "/tmp/DBD-MariaDB-${DBD_MARIADB_VERSION}" \
 && perl Makefile.PL \
 && make \
 && make test \
 && make install \
 && cd "/tmp/PatchReader-${PATCHREADER_VERSION}" \
 && perl Makefile.PL \
 && make \
 && make test \
 && make install \
 && cd / \
 && rm -rf \
      "/tmp/Template-Toolkit-${TEMPLATE_TOOLKIT_VERSION}" \
      "/tmp/Template-Toolkit-${TEMPLATE_TOOLKIT_VERSION}.tar.gz" \
      "/tmp/DBD-MariaDB-${DBD_MARIADB_VERSION}" \
      "/tmp/DBD-MariaDB-${DBD_MARIADB_VERSION}.tar.gz" \
      "/tmp/PatchReader-${PATCHREADER_VERSION}" \
      "/tmp/PatchReader-${PATCHREADER_VERSION}.tar.gz" \
 && apt-get purge -y --auto-remove build-essential \
 && rm -rf /var/lib/apt/lists/*

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
COPY --chmod=0755 containers/bugzilla/bridge.pl /usr/local/bin/bzr-live-bridge

RUN a2enmod expires headers rewrite \
 && a2dismod mpm_event \
 && a2enmod mpm_prefork

EXPOSE 80
ENTRYPOINT ["/usr/local/bin/bzr-live-bugzilla"]
