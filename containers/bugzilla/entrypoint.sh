#!/usr/bin/env bash
set -euo pipefail

required=(
  BZ_ADMIN_EMAIL BZ_ADMIN_PASSWORD BZ_ADMIN_REALNAME
  BZ_ALLOW_UNSAFE_UTF8_CONVERSION BZ_DB_HOST BZ_DB_NAME
  BZ_DB_PASSWORD BZ_DB_PORT BZ_DB_USER BZ_URLBASE
)
for name in "${required[@]}"; do
  [[ -n ${!name:-} ]] || {
    printf 'bugzilla setup failed: required environment variable %s is empty\n' "$name" >&2
    exit 64
  }
done
[[ $BZ_ALLOW_UNSAFE_UTF8_CONVERSION == 0 || $BZ_ALLOW_UNSAFE_UTF8_CONVERSION == 1 ]] || {
  printf 'bugzilla setup failed: BZ_ALLOW_UNSAFE_UTF8_CONVERSION must be 0 or 1\n' >&2
  exit 64
}

export MYSQL_PWD=$BZ_DB_PASSWORD
ready=0
for _ in $(seq 1 120); do
  if mariadb-admin ping --silent \
    --host="$BZ_DB_HOST" --port="$BZ_DB_PORT" --user="$BZ_DB_USER"; then
    ready=1
    break
  fi
  sleep 2
done
[[ $ready == 1 ]] || {
  printf 'bugzilla setup failed: database was not ready within 240 seconds\n' >&2
  exit 1
}
unset MYSQL_PWD

cd /var/www/html
answers=$(mktemp /tmp/bugzilla-checksetup.XXXXXX)
trap 'rm -f "$answers"' EXIT
chmod 600 "$answers"
perl -Mstrict -Mwarnings -e '
  my ($template, $output) = @ARGV;
  open my $in, "<", $template or die "cannot open setup template: $!\n";
  local $/;
  my $text = <$in>;
  close $in or die "cannot close setup template: $!\n";
  for my $name (qw(
    BZ_ADMIN_EMAIL BZ_ADMIN_PASSWORD BZ_ADMIN_REALNAME
    BZ_ALLOW_UNSAFE_UTF8_CONVERSION BZ_DB_HOST BZ_DB_NAME
    BZ_DB_PASSWORD BZ_DB_PORT BZ_DB_USER BZ_URLBASE
  )) {
    my $value = $ENV{$name};
    $value =~ s/\\/\\\\/g;
    $value =~ s/\x27/\\\x27/g;
    $text =~ s/__${name}__/$value/g;
  }
  open my $out, ">", $output or die "cannot create setup answers: $!\n";
  print {$out} $text or die "cannot write setup answers: $!\n";
  close $out or die "cannot close setup answers: $!\n";
' /opt/bzr-live/checksetup_answers.txt "$answers"

perl checksetup.pl "$answers"
rm -f "$answers"
trap - EXIT
chown -R www-data:www-data /var/www/html/data
exec apachectl -D FOREGROUND
