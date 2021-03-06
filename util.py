# coding=utf-8
"""Misc utility constants and classes.
"""

import collections
import datetime
import json
import mimetypes
import re
import urllib
import urlparse

import requests
import webapp2

from oauth_dropins.webutil.models import StringIdModel
from oauth_dropins.webutil.util import *
from granary import source
from appengine_config import HTTP_TIMEOUT, DEBUG

from google.appengine.api import mail
from google.appengine.api import memcache
from google.appengine.api import taskqueue
from google.appengine.ext import ndb

# when running in dev_appserver, replace these domains in links with localhost
LOCALHOST_TEST_DOMAINS = frozenset(('kylewm.com', 'snarfed.org'))

EPOCH = datetime.datetime.utcfromtimestamp(0)
POLL_TASK_DATETIME_FORMAT = '%Y-%m-%d-%H-%M-%S'
FAILED_RESOLVE_URL_CACHE_TIME = 60 * 60 * 24  # a day

# rate limiting errors. twitter returns 429, instagram 503, google+ 403.
# TODO: facebook. it returns 200 and reports the error in the response.
# https://developers.facebook.com/docs/reference/ads-api/api-rate-limiting/
HTTP_RATE_LIMIT_CODES = frozenset(('403', '429', '503'))

USER_AGENT_HEADER = {'User-Agent': 'Bridgy (http://brid.gy/about)'}

# Domains that don't support webmentions. Mainly just the silos.
# Subdomains are automatically blacklisted too.
#
# We also check this when a user sign up and we extract the web site links from
# their profile. We automatically omit links to these domains.
with open('domain_blacklist.txt') as f:
  BLACKLIST = {l.strip() for l in f
               if l.strip() and not l.strip().startswith('#')}

Website = collections.namedtuple('Website', ('url', 'domain'))


def add_poll_task(source, now=False, **kwargs):
  """Adds a poll task for the given source entity.

  Pass now=True to insert a poll-now task.

  Tasks inserted from a backend (e.g. twitter_streaming) are sent to that
  backend by default, which doesn't work in the dev_appserver. Setting the
  target version to 'default' in queue.yaml doesn't work either, but setting it
  here does.

  Note the constant. The string 'default' works in dev_appserver, but routes to
  default.brid-gy.appspot.com in prod instead of www.brid.gy, which breaks SSL
  because appspot.com doesn't have a third-level wildcard cert.
  """
  last_polled_str = source.last_polled.strftime(POLL_TASK_DATETIME_FORMAT)
  queue = 'poll-now' if now else 'poll'
  task = taskqueue.add(queue_name=queue,
                       params={'source_key': source.key.urlsafe(),
                               'last_polled': last_polled_str},
                       **kwargs)
  logging.info('Added %s task %s with args %s', queue, task.name, kwargs)


def add_propagate_task(entity, **kwargs):
  """Adds a propagate task for the given response entity.
  """
  task = taskqueue.add(queue_name='propagate',
                       params={'response_key': entity.key.urlsafe()},
                       target=taskqueue.DEFAULT_APP_VERSION,
                       **kwargs)
  logging.info('Added propagate task: %s', task.name)


def add_propagate_blogpost_task(entity, **kwargs):
  """Adds a propagate-blogpost task for the given response entity.
  """
  task = taskqueue.add(queue_name='propagate-blogpost',
                       params={'key': entity.key.urlsafe()},
                       target=taskqueue.DEFAULT_APP_VERSION,
                       **kwargs)
  logging.info('Added propagate-blogpost task: %s', task.name)


def email_me(**kwargs):
  """Thin wrapper around mail.send_mail() that handles errors."""
  try:
    mail.send_mail(sender='admin@brid-gy.appspotmail.com',
                   to='webmaster@brid.gy', **kwargs)
  except BaseException:
    logging.warning('Error sending notification email', exc_info=True)


def requests_get(url, **kwargs):
  """Wraps requests.get and injects our timeout and user agent."""
  kwargs.setdefault('headers', {}).update(USER_AGENT_HEADER)
  kwargs.setdefault('timeout', HTTP_TIMEOUT)
  return requests.get(url, **kwargs)


def follow_redirects(url, cache=True):
  """Fetches a URL with HEAD, repeating if necessary to follow redirects.

  Caches resolved URLs in memcache by default. *Does not* raise an exception if
  any of the HTTP requests fail, just returns the failed response. If you care,
  be sure to check the returned response's status code!

  Args:
    url: string
    cache: whether to read/write memcache

  Returns:
    the requests.Response for the final request
  """
  if cache:
    cache_key = 'R ' + url
    resolved = memcache.get(cache_key)
    if resolved is not None:
      return resolved

  # can't use urllib2 since it uses GET on redirect requests, even if i specify
  # HEAD for the initial request.
  # http://stackoverflow.com/questions/9967632
  try:
    # default scheme to http
    parsed = urlparse.urlparse(url)
    if not parsed.scheme:
      url = 'http://' + url
    resolved = requests.head(url, allow_redirects=True, timeout=HTTP_TIMEOUT,
                             headers=USER_AGENT_HEADER)
    resolved.raise_for_status()
    cache_time = 0  # forever
  except AssertionError:
    raise
  except BaseException, e:
    logging.warning("Couldn't resolve URL %s : %s", url, e)
    resolved = requests.Response()
    resolved.url = url
    resolved.status_code = 499  # not standard. i made this up.
    cache_time = FAILED_RESOLVE_URL_CACHE_TIME

  content_type = resolved.headers.get('content-type')
  if not content_type:
    type, _ = mimetypes.guess_type(resolved.url)
    resolved.headers['content-type'] = type or 'text/html'

  refresh = resolved.headers.get('refresh')
  if refresh:
    for part in refresh.split(';'):
      if part.strip().startswith('url='):
        return follow_redirects(part.strip()[4:])

  if cache:
    memcache.set(cache_key, resolved, time=cache_time)
  return resolved


# Wrap webutil.util.tag_uri and hard-code the year to 2013.
#
# Needed because I originally generated tag URIs with the current year, which
# resulted in different URIs for the same objects when the year changed. :/
from oauth_dropins.webutil import util
_orig_tag_uri = tag_uri
util.tag_uri = lambda domain, name: _orig_tag_uri(domain, name, year=2013)


def get_webmention_target(url, cache=True):
  """Resolves a URL and decides whether we should try to send it a webmention.

  Note that this ignores failed HTTP requests, ie the boolean in the returned
  tuple will be true! TODO: check callers and reconsider this.

  Args:
    url: string
    cache: whether to use memcache when following redirects

  Returns: (string url, string pretty domain, boolean) tuple. The boolean is
    True if we should send a webmention, False otherwise, e.g. if it's a bad
    URL, not text/html, or in the blacklist.
  """
  try:
    domain = domain_from_link(url).lower()
  except BaseException:
    logging.warning('Dropping bad URL %s.', url)
    return (url, None, False)

  if not domain or in_webmention_blacklist(domain):
    return (url, domain, False)

  resolved = follow_redirects(url, cache=cache)
  if resolved.url != url:
    logging.debug('Resolved %s to %s', url, resolved.url)
    url = resolved.url
    domain = domain_from_link(url)

  is_html = resolved.headers.get('content-type', '').startswith('text/html')
  return (clean_webmention_url(url), domain, is_html)


def in_webmention_blacklist(domain):
  """Returns True if the domain or its root domain is in BLACKLIST."""
  return (domain in BLACKLIST or
          # strip subdomain and check again
          (domain and '.'.join(domain.split('.')[-2:]) in BLACKLIST))


def clean_webmention_url(url):
  """Removes transient query params (e.g. utm_*) from a webmention target URL.

  The utm_* (Urchin Tracking Metrics?) params come from Google Analytics.
  https://support.google.com/analytics/answer/1033867

  Args:
    url: string

  Returns: string, the cleaned url
  """
  utm_params = set(('utm_campaign', 'utm_content', 'utm_medium', 'utm_source',
                    'utm_term'))
  parts = list(urlparse.urlparse(url))
  query = urllib.unquote_plus(parts[4].encode('utf-8'))
  params = [(name, value) for name, value in urlparse.parse_qsl(query)
            if name not in utm_params]
  parts[4] = urllib.urlencode(params)
  return urlparse.urlunparse(parts)


def prune_activity(activity):
  """Returns an activity dict with just id, url, content, to, and object.

  If the object field exists, it's pruned down to the same fields. Any fields
  duplicated in both the activity and the object are removed from the object.

  Note that this only prunes the to field if it says the activity is public,
  since granary.Source.is_public() defaults to saying an activity is
  public if the to field is missing. If that ever changes, we'll need to
  start preserving the to field here.

  Args:
    activity: ActivityStreams activity dict

  Returns: pruned activity dict
  """
  keep = ['id', 'url', 'content']
  if not source.Source.is_public(activity):
    keep += ['to']
  pruned = {f: activity.get(f) for f in keep}

  obj = activity.get('object')
  if obj:
    obj = pruned['object'] = prune_activity(obj)
    for k, v in obj.items():
      if pruned.get(k) == v:
        del obj[k]

  return trim_nulls(pruned)


def replace_test_domains_with_localhost(url):
  """Replace domains in LOCALHOST_TEST_DOMAINS with localhost for local
  testing when in DEBUG mode.

  Args:
    url: a string

  Returns: a string with certain well-known domains replaced by localhost
  """
  if url and DEBUG:
    for test_domain in LOCALHOST_TEST_DOMAINS:
      url = re.sub('https?://' + test_domain,
                   'http://localhost', url)
  return url


class Handler(webapp2.RequestHandler):
  """Includes misc request handler utilities.

  Attributes:
    messages: list of notification messages to be rendered in this page or
      wherever it redirects
  """

  def __init__(self, *args, **kwargs):
    super(Handler, self).__init__(*args, **kwargs)
    self.messages = set()

  def redirect(self, uri, **kwargs):
    """Adds self.messages to the fragment, separated by newlines.
    """
    parts = list(urlparse.urlparse(uri))
    if self.messages and not parts[5]:  # parts[5] is fragment
      parts[5] = '!' + urllib.quote('\n'.join(self.messages).encode('utf-8'))
    uri = urlparse.urlunparse(parts)
    super(Handler, self).redirect(uri, **kwargs)

  def maybe_add_or_delete_source(self, source_cls, auth_entity, state, **kwargs):
    """Adds or deletes a source if auth_entity is not None.

    Used in each source's oauth-dropins CallbackHandler finish() and get()
    methods, respectively.

    Args:
      source_cls: source class, e.g. Instagram
      auth_entity: ouath-dropins auth entity
      state: string, OAuth callback state parameter. a JSON serialized dict
        with operation, feature, and an optional callback URL. For deletes,
        it will also include the source key
      kwargs: passed through to the source_cls constructor

    Returns:
      source entity if it was created or updated, otherwise None
    """
    state_obj = self.decode_state_parameter(state)
    operation = state_obj.get('operation', 'add')
    feature = state_obj.get('feature')
    callback = state_obj.get('callback')
    user_url = state_obj.get('user_url')

    logging.debug(
      'maybe_add_or_delete_source with operation=%s, feature=%s, callback=%s',
      operation, feature, callback)

    if operation == 'add':  # this is an add/update
      if not auth_entity:
        if not self.messages:
          self.messages.add("OK, you're not signed up. Hope you reconsider!")
        if callback:
          callback = util.add_query_params(callback, {'result': 'declined'})
          logging.debug(
            'user declined adding source, redirect to external callback %s',
            callback)
          # call super.redirect so the callback url is unmodified
          super(Handler, self).redirect(callback.encode('utf-8'))
        else:
          self.redirect('/')
        return

      CachedPage.invalidate('/users')
      logging.info('%s.create_new with %s', source_cls.__class__.__name__,
                   (auth_entity.key, state, kwargs))
      source = source_cls.create_new(self, auth_entity=auth_entity,
                                     features=[feature] if feature else [],
                                     user_url=user_url, **kwargs)
      if callback:
        callback = util.add_query_params(callback, {
          'result': 'success',
          'user': source.bridgy_url(self),
          'key': source.key.urlsafe(),
        } if source else {'result': 'failure'})
        logging.debug(
          'finished adding source, redirect to external callback %s', callback)
        # call super.redirect so the callback url is unmodified
        super(Handler, self).redirect(callback.encode('utf-8'))
      else:
        self.redirect(source.bridgy_url(self) if source else '/')
      return source

    else:  # this is a delete
      if auth_entity:
        self.redirect('/delete/finish?auth_entity=%s&state=%s' %
                      (auth_entity.key.urlsafe(), state))
      else:
        self.messages.add('If you want to disable, please approve the %s prompt.' %
                          source_cls.GR_CLASS.NAME)
        self.redirect_home_or_user_page(state)

  def construct_state_param_for_add(self, state=None, **kwargs):
    """Construct the state parameter if one isn't explicitly passed in
    """
    if not state:
      state = self.encode_state_parameter({
        'operation': 'add',
        'feature': kwargs.get('feature') or self.request.get('feature'),
        'callback': kwargs.get('callback') or self.request.get('callback'),
        'user_url': kwargs.get('user_url') or self.request.get('user_url'),
      })
    return state

  def encode_state_parameter(self, obj):
    """The state parameter is passed to various source authorization
    endpoints and returned in a callback. This encodes a JSON object
    so that it can be safely included as a query string parameter.

    The following keys are common:
      - operation: 'add' or 'delete'
      - feature: 'listen', 'publish', or 'webmention'
      - callback: an optional external callback, that we will redirect to at
                  the end of the authorization handshake
      - source: the source key, only applicable to deletes

    Args:
      obj: a JSON-serializable dict

    Returns: a string
    """
    # pass in custom separators to cut down on whitespace, and sort keys for
    # unit test consistency
    return json.dumps(trim_nulls(obj), separators=(',', ':'), sort_keys=True)

  def decode_state_parameter(self, state):
    """The state parameter is passed to various source authorization
    endpoints and returned in a callback. This decodes a
    JSON-serialized string and returns a dict.

    See encode_state_parameter for a list of common state parameter
    keys.

    Args:
      state: a string (JSON-serialized dict)

    Returns: a dict containing operation, feature, and possibly other fields
    """
    logging.debug('decoding state "%s"' % state)
    obj = json.loads(state) if state else {}
    if not isinstance(obj, dict):
      logging.error('got a non-dict state parameter %s', state)
      return None
    return obj

  def redirect_home_or_user_page(self, state):
    redirect_to = '/'
    split = state.split('-', 1)
    if len(split) >= 2:
      source = ndb.Key(urlsafe=split[1]).get()
      if source:
        redirect_to = source.bridgy_url(self)
    self.redirect(redirect_to)

  def preprocess_source(self, source):
    """Prepares a source entity for rendering in the source.html template.

    - use id as name if name isn't provided
    - convert image URLs to https if we're serving over SSL
    - zip domain_urls and domains into website field, list of Website
      namedtuples with url and domain fields

    Args:
      source: Source entity
    """
    if not source.name:
      source.name = source.key.string_id()
    if source.picture:
      source.picture = util.update_scheme(source.picture, self)
    source.websites = [Website(url=u, domain=d) for u, d in
                       zip(source.domain_urls, source.domains)]
    return source


def oauth_starter(oauth_start_handler):
  """Returns an oauth-dropins start handler that injects the state param.

  Args:
    oauth_start_handler: oauth-dropins StartHandler to use,
      e.g. oauth_dropins.twitter.StartHandler.
  """
  class StartHandler(oauth_start_handler, Handler):
    def redirect_url(self, state=None):
      return super(StartHandler, self).redirect_url(
        self.construct_state_param_for_add(state))

  return StartHandler


class CachedPage(StringIdModel):
  """Cached HTML for pages that changes rarely. Key id is path.

  Stored in the datastore since datastore entities in memcache (mostly
  Responses) are requested way more often, so it would get evicted
  out of memcache easily.

  Keys, useful for deleting from memcache:
  /: aglzfmJyaWQtZ3lyEQsSCkNhY2hlZFBhZ2UiAS8M
  /users: aglzfmJyaWQtZ3lyFgsSCkNhY2hlZFBhZ2UiBi91c2Vycww
  """
  html = ndb.TextProperty()
  expires = ndb.DateTimeProperty()

  @classmethod
  def load(cls, path):
    cached = CachedPage.get_by_id(path)
    if cached:
      if cached.expires and datetime.datetime.now() > cached.expires:
        logging.info('Deleting expired cached page for %s', path)
        cached.key.delete()
        return None
      else:
        logging.info('Found cached page for %s', path)
    return cached

  @classmethod
  def store(cls, path, html, expires=None):
    """path and html are strings, expires is a datetime.timedelta."""
    logging.info('Storing new page in cache for %s', path)
    if expires is not None:
      logging.info('  (expires in %s)', expires)
      expires = datetime.datetime.now() + expires
    CachedPage(id=path, html=html, expires=expires).put()

  @classmethod
  def invalidate(cls, path):
    logging.info('Deleting cached page for %s', path)
    CachedPage(id=path).key.delete()
