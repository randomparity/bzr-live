#!/usr/bin/perl
# bzr-live fixed-operation provisioning bridge (issue #4, ADR 0004).
# One allowlisted operation per invocation: argv[0] = operation, stdin = one JSON
# request, stdout = one JSON reply {"ok": true, "result": ...} or
# {"ok": false, "error": "..."}. Bugzilla object layer only — never raw SQL.
use strict;
use warnings;
use lib qw(/var/www/html /var/www/html/lib);

use JSON::XS qw(decode_json encode_json);

BEGIN { chdir '/var/www/html' or die "cannot chdir to bugzilla root: $!\n"; }

use Bugzilla;
use Bugzilla::Constants;
use Bugzilla::Component;
use Bugzilla::Field;
use Bugzilla::Field::Choice;
use Bugzilla::FlagType;
use Bugzilla::Keyword;
use Bugzilla::Milestone;
use Bugzilla::Product;
use Bugzilla::User;
use Bugzilla::User::APIKey;
use Bugzilla::Version;

my %FIELD_TYPES = (
  'text'          => FIELD_TYPE_FREETEXT,
  'single-select' => FIELD_TYPE_SINGLE_SELECT,
  'multi-select'  => FIELD_TYPE_MULTI_SELECT,
);
my %FIELD_TYPE_NAMES = reverse %FIELD_TYPES;

sub reply_ok    { print encode_json({ok => JSON::XS::true, result => $_[0]}); exit 0 }
sub reply_error { print encode_json({ok => JSON::XS::false, error => "$_[0]"}); exit 1 }

my %OPERATIONS = map { $_ => 1 } qw(
  create-version create-milestone create-custom-field create-keyword
  create-flag-type create-api-key get-custom-field get-keyword get-flag-type
);

my $operation = $ARGV[0] // '';
unless ($OPERATIONS{$operation}) {
  print STDERR "unknown operation\n";
  exit 2;
}

my $request = eval {
  local $/;
  my $raw = <STDIN> // '';
  length $raw ? decode_json($raw) : {};
};
reply_error("request was not valid JSON") if $@;

my $result = eval {
  Bugzilla->usage_mode(USAGE_MODE_CMDLINE);
  my $admin = Bugzilla::User->check({name => $ENV{BZ_ADMIN_EMAIL}});
  Bugzilla->set_user($admin);
  dispatch($operation, $request);
};
reply_error($@) if $@;
reply_ok($result);

sub product_of { Bugzilla::Product->check({name => $_[0]}) }

sub inclusion_pairs {
  # canonical [{product, component}] with nulls meaning any -> "pid:cid" strings
  my ($pairs) = @_;
  my @clusions;
  for my $pair (@{$pairs // []}) {
    my ($pid, $cid) = ('', '');
    if (defined $pair->{product}) {
      my $product = product_of($pair->{product});
      $pid = $product->id;
      if (defined $pair->{component}) {
        my $component = Bugzilla::Component->check(
          {product => $product, name => $pair->{component}});
        $cid = $component->id;
      }
    }
    push @clusions, "$pid:$cid";
  }
  @clusions = (':') unless @clusions;
  return \@clusions;
}

sub stored_inclusions {
  # FlagType->inclusions: {"Product:Component" => "pid:cid"}; __Any__ -> null
  my ($flagtype) = @_;
  my @pairs;
  for my $name (keys %{$flagtype->inclusions}) {
    my ($product, $component) = split /:/, $name, 2;
    push @pairs, {
      product   => ($product eq '__Any__' ? undef : $product),
      component => (!defined $component || $component eq '__Any__'
                    ? undef : $component),
    };
  }
  return \@pairs;
}

sub dispatch {
  my ($operation, $request) = @_;

  if ($operation eq 'create-version') {
    # Version/Milestone use NAME_FIELD 'value'; passing 'name' is an invalid
    # column at the pinned SHA (Version.pm:30-56, Object.pm create).
    my $version = Bugzilla::Version->create(
      {value => $request->{name}, product => product_of($request->{product})});
    return {name => $version->name};
  }
  if ($operation eq 'create-milestone') {
    my $milestone = Bugzilla::Milestone->create(
      {value => $request->{name}, product => product_of($request->{product})});
    return {name => $milestone->name};
  }
  if ($operation eq 'create-keyword') {
    my $keyword = Bugzilla::Keyword->create(
      {name => $request->{name}, description => $request->{description}});
    return {name => $keyword->name};
  }
  if ($operation eq 'create-custom-field') {
    my $type = $FIELD_TYPES{$request->{field_type} // ''}
      // die "unsupported field_type\n";
    my $field = Bugzilla::Field->create({
      name        => $request->{name},
      description => $request->{name},
      type        => $type,
      custom      => 1,
      enter_bug   => 1,
    });
    if ($type != FIELD_TYPE_FREETEXT) {
      Bugzilla::Field::Choice->type($field)->create({value => $_})
        for @{$request->{values} // []};
    }
    return {name => $field->name};
  }
  if ($operation eq 'create-flag-type') {
    my $flagtype = Bugzilla::FlagType->create({
      name             => $request->{name},
      description      => $request->{description},
      target_type      => $request->{target},
      cc_list          => '',
      sortkey          => 1,
      is_active        => 1,
      is_requestable   => 1,
      is_requesteeble  => 1,
      is_multiplicable => 1,
      inclusions       => inclusion_pairs($request->{inclusions}),
    });
    return {name => $flagtype->name};
  }
  if ($operation eq 'create-api-key') {
    my $login = $request->{login} // $ENV{BZ_ADMIN_EMAIL};
    my $user  = Bugzilla::User->check({name => $login});
    my $key   = Bugzilla::User::APIKey->create(
      {user_id => $user->id, description => 'bzr-live provisioning'});
    return {login => $user->login, api_key => $key->api_key};
  }
  if ($operation eq 'get-custom-field') {
    my $field = Bugzilla::Field->new({name => $request->{name}});
    return undef unless $field && $field->custom;
    my $values = [];
    if ($field->is_select) {
      $values = [grep { $_ ne '---' }
                 map { $_->name } @{$field->legal_values}];
    }
    return {
      name       => $field->name,
      field_type => $FIELD_TYPE_NAMES{$field->type} // 'unknown',
      values     => $values,
    };
  }
  if ($operation eq 'get-keyword') {
    my $keyword = Bugzilla::Keyword->new({name => $request->{name}});
    return undef unless $keyword;
    return {name => $keyword->name, description => $keyword->description};
  }
  if ($operation eq 'get-flag-type') {
    my $matches = Bugzilla::FlagType::match({name => $request->{name}});
    return undef unless @$matches;
    die "flag type name is not unique in the fixture\n" if @$matches > 1;
    my $flagtype = $matches->[0];
    return {
      name        => $flagtype->name,
      description => $flagtype->description,
      # the accessor already maps the stored 'b'/'a' to 'bug'/'attachment'
      # (FlagType.pm:272 at the pinned SHA)
      target      => $flagtype->target_type,
      inclusions  => stored_inclusions($flagtype),
    };
  }
  die "unreachable operation\n";
}
