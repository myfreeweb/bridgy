"""Google+ source code and datastore model classes.
"""

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']

import datetime
import json

import appengine_config

from granary import googleplus as gr_googleplus
from oauth_dropins import googleplus as oauth_googleplus
import models
import util

from google.appengine.ext import ndb
import webapp2


class GooglePlusPage(models.Source):
  """A Google+ profile or page.

  The key name is the user id.
  """

  GR_CLASS = gr_googleplus.GooglePlus
  SHORT_NAME = 'googleplus'

  # We're currently close to the G+ API's daily limit of 10k requests per day.
  # So low! :/ Usage history:
  # QPS: https://cloud.google.com/console/project/1029605954231
  # Today's quota usage: https://code.google.com/apis/console/b/0/?noredirect#project:1029605954231:quotas
  # Daily total usage: https://code.google.com/apis/console/b/0/?pli=1#project:1029605954231:stats
  FAST_POLL = datetime.timedelta(minutes=20)
  # API quotas are refilled daily. Use 30h to make sure we're over a day even
  # after the randomized task ETA.
  RATE_LIMITED_POLL = datetime.timedelta(hours=30)

  type = ndb.StringProperty(choices=('user', 'page'))

  @staticmethod
  def new(handler, auth_entity=None, **kwargs):
    """Creates and returns a GooglePlusPage for the logged in user.

    Args:
      handler: the current RequestHandler
      auth_entity: oauth_dropins.googleplus.GooglePlusAuth
    """
    # Google+ Person resource
    # https://developers.google.com/+/api/latest/people#resource
    user = json.loads(auth_entity.user_json)
    type = 'user' if user.get('objectType', 'person') == 'person' else 'page'

    # override the sz param to ask for a 128x128 image. if there's an existing
    # sz query param (there usually is), the new one will come afterward and
    # override it.
    picture = user.get('image', {}).get('url')
    picture = util.add_query_params(picture, {'sz': '128'})

    return GooglePlusPage(id=user['id'],
                          auth_entity=auth_entity.key,
                          url=user.get('url'),
                          name=user.get('displayName'),
                          picture=picture,
                          type=type,
                          **kwargs)

  def silo_url(self):
    """Returns the Google+ account URL, e.g. https://plus.google.com/+Foo."""
    return self.url

  def __getattr__(self, name):
    """Overridden to pass auth_entity to gr_googleplus.GooglePlus's ctor."""
    if name == 'gr_source' and self.auth_entity:
      self.gr_source = gr_googleplus.GooglePlus(auth_entity=self.auth_entity.get())
      return self.gr_source

    return getattr(super(GooglePlusPage, self), name)

  def poll_period(self):
    """Returns the poll frequency for this source."""
    return (self.RATE_LIMITED_POLL if self.rate_limited
            else super(GooglePlusPage, self).poll_period())

  def canonicalize_syndication_url(self, url):
    """Follow redirects to find and use profile nicknames instead of ids.

    ...e.g. +RyanBarrett in https://plus.google.com/+RyanBarrett/posts/JPpA8mApAv2.
    """
    return super(GooglePlusPage, self).canonicalize_syndication_url(
      util.follow_redirects(url).url)


class OAuthCallback(util.Handler):
  """OAuth callback handler.

  Both the add and delete flows have to share this because Google+'s
  oauth-dropin doesn't yet allow multiple callback handlers. :/
  """
  def get(self):
    auth_entity_str_key = util.get_required_param(self, 'auth_entity')
    state = self.request.get('state')
    if not state:
      # state doesn't currently come through for G+. not sure why. doesn't
      # matter for now since we don't plan to implement publish for G+.
      state = self.construct_state_param_for_add(feature='listen')
    auth_entity = ndb.Key(urlsafe=auth_entity_str_key).get()
    self.maybe_add_or_delete_source(GooglePlusPage, auth_entity, state)


application = webapp2.WSGIApplication([
    # OAuth scopes are set in listen.html and publish.html
    ('/googleplus/start', util.oauth_starter(oauth_googleplus.StartHandler).to(
      '/googleplus/oauth2callback')),
    ('/googleplus/oauth2callback', oauth_googleplus.CallbackHandler.to('/googleplus/add')),
    ('/googleplus/add', OAuthCallback),
    ('/googleplus/delete/start', oauth_googleplus.StartHandler.to('/googleplus/oauth2callback')),
    ], debug=appengine_config.DEBUG)
