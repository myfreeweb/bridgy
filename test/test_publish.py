# coding=utf-8
"""Unit tests for publish.py.
"""

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']

import json
import urllib

import appengine_config

from granary import source as gr_source
from google.appengine.api import mail
import mox
import requests
import webapp2
from webob import exc

import facebook
from models import Publish, PublishedPage
import publish
import testutil
import util


class PublishTest(testutil.HandlerTest):

  def setUp(self):
    super(PublishTest, self).setUp()
    publish.SOURCE_NAMES['fake'] = testutil.FakeSource
    publish.SOURCE_DOMAINS['fa.ke'] = testutil.FakeSource

    self.auth_entity = testutil.FakeAuthEntity(id='0123456789')
    self.source = testutil.FakeSource(
      id='foo.com', features=['publish'], domains=['foo.com'],
      domain_urls=['http://foo.com/'], auth_entity=self.auth_entity.key)
    self.source.put()

    self.oauth_state = {
      'source_url': 'http://foo.com/bar',
      'target_url': 'http://brid.gy/publish/fake',
      'source_key': self.source.key.urlsafe(),
      'bridgy_omit_link': False,
    }
    self.post_html = '<article class="h-entry"><p class="e-content">%s</p></article>'
    self.backlink = '\n<a href="http://localhost/publish/fake"></a>'

  def get_response(self, source=None, target=None, preview=False,
                   interactive=False, params=None):
    if params is None:
      params = {}
    params.update({
      'source': source or 'http://foo.com/bar',
      'target': target or 'http://brid.gy/publish/fake',
      'source_key': self.source.key.urlsafe(),
      })

    app = publish.application
    assert not (preview and interactive)
    if interactive:
      class FakeSendHandler(publish.SendHandler):
        def post(fsh_self):
          state = (self.handler.encode_state_parameter(self.oauth_state)
                   if self.oauth_state else None)
          fsh_self.finish(self.auth_entity, state)
      app = webapp2.WSGIApplication([('.*', FakeSendHandler)])

    return app.get_response(
      '/publish/preview' if preview else '/publish/webmention',
      method='POST', body=urllib.urlencode(params))

  def expect_requests_get(self, url, body='', backlink=None, **kwargs):
    body += backlink or self.backlink
    resp = super(PublishTest, self).expect_requests_get(url, body, **kwargs)
    return resp

  def assert_response(self, expected, status=None, preview=False, **kwargs):
    resp = self.get_response(preview=preview, **kwargs)
    self.assertEquals(status, resp.status_int)
    if preview:
      self.assertIn(expected, resp.body.decode('utf-8'),
                    '%r\n\n=== vs ===\n\n%r' % (expected, resp.body))
    else:
      self.assertIn(expected, json.loads(resp.body)[
        'content' if status == 200 else 'error'])
    return resp

  def assert_success(self, expected, **kwargs):
    return self.assert_response(expected, status=200, **kwargs)

  def assert_error(self, expected, status=400, **kwargs):
    return self.assert_response(expected, status=status, **kwargs)

  def test_webmention_success(self):
    self.expect_requests_get('http://foo.com/bar', self.post_html % 'foo')
    self.mox.ReplayAll()
    self.assert_success('foo - http://foo.com/bar', interactive=False)
    self._check_entity()

  def test_interactive_success(self):
    self.expect_requests_get('http://foo.com/bar', self.post_html % 'foo')
    self.mox.ReplayAll()

    resp = self.get_response(interactive=True)
    self.assertEquals(302, resp.status_int)
    self.assertEquals(
      'http://localhost/fake/foo.com#!'
        'Done! <a href="http://fake/url">Click here to view.</a>',
      urllib.unquote_plus(resp.headers['Location']))
    self._check_entity()

  def _check_entity(self):
    self.assertTrue(PublishedPage.get_by_id('http://foo.com/bar'))
    publish = Publish.query().get()
    self.assertEquals(self.source.key, publish.source)
    self.assertEquals('complete', publish.status)
    self.assertEquals('post', publish.type)
    self.assertEquals('FakeSource post label', publish.type_label)
    expected_html = (self.post_html % 'foo') + self.backlink
    self.assertEquals(expected_html, publish.html)
    self.assertEquals({'id': 'fake id', 'url': 'http://fake/url',
                       'content': 'foo - http://foo.com/bar'},
                      publish.published)

  def test_interactive_from_wrong_user_page(self):
    other_source = testutil.FakeSource.new(None).put()
    self.oauth_state['source_key'] = other_source.urlsafe()

    resp = self.get_response(interactive=True)
    self.assertEquals(302, resp.status_int)
    self.assertEquals(
      'http://localhost/fake/%s#!'
        'Please log into FakeSource as fake to publish that page.' %
        other_source.id(),
      urllib.unquote_plus(resp.headers['Location']))

    self.assertIsNone(Publish.query().get())

  def test_interactive_oauth_decline(self):
    self.auth_entity = None
    resp = self.get_response(interactive=True)
    self.assertEquals(302, resp.status_int)
    self.assertEquals(
      'http://localhost/fake/foo.com#!'
        'If you want to publish or preview, please approve the prompt.',
      urllib.unquote_plus(resp.headers['Location']))

    self.assertIsNone(Publish.query().get())

  def test_interactive_no_state(self):
    """https://github.com/snarfed/bridgy/issues/449"""
    self.oauth_state = None
    resp = self.get_response(interactive=True)
    self.assertEquals(302, resp.status_int)
    self.assertEquals(
      'http://localhost/#!'
        'If you want to publish or preview, please approve the prompt.',
      urllib.unquote_plus(resp.headers['Location']))

    self.assertIsNone(Publish.query().get())

  def test_success_domain_translates_to_lowercase(self):
    self.expect_requests_get('http://FoO.cOm/Bar', self.post_html % 'foo')
    self.mox.ReplayAll()
    self.assert_success('foo - http://FoO.cOm/Bar', source='http://FoO.cOm/Bar')

  def test_success_source_status_error(self):
    """Sources in status 'error' should still be able to publish."""
    self.source.status = 'error'
    self.source.put()

    self.expect_requests_get('http://foo.com/bar', self.post_html % 'foo')
    self.mox.ReplayAll()
    self.assert_success('foo - http://foo.com/bar')

  def test_already_published(self):
    """We shouldn't allow duplicating an existing, *completed* publish."""
    page = PublishedPage(id='http://foo.com/bar')

    # these are all fine
    Publish(parent=page.key, source=self.source.key, status='new').put()
    Publish(parent=page.key, source=self.source.key, status='failed').put()
    Publish(parent=page.key, source=self.source.key, status='complete',
            type='preview').put()

    for i in range(2):
      self.expect_requests_get('http://foo.com/bar', self.post_html % 'foo')
    self.mox.ReplayAll()

    # first attempt should work
    self.assert_success('foo - http://foo.com/bar')
    self.assertEquals(4, Publish.query().count())
    self.assertEquals(2, Publish.query(Publish.status == 'complete').count())

    # now that there's a complete Publish entity, more attempts should fail
    self.assert_error("Sorry, you've already published that page")
    # try again to test for a bug we had where a second try would succeed
    self.assert_error("Sorry, you've already published that page")
    # should still be able to preview though
    self.assert_success('preview of foo', preview=True)

  def test_more_than_one_silo(self):
    """POSSE to more than one silo should not trip the
    'already published' check"""

    class FauxSource(testutil.FakeSource):
      SHORT_NAME = 'faux'

    publish.SOURCE_NAMES['faux'] = FauxSource
    FauxSource(
      id='foo.com', features=['publish'], domains=['foo.com'],
      domain_urls=['http://foo.com/']).put()

    html = self.post_html % 'foo'
    self.expect_requests_get('http://foo.com/bar', html)
    self.expect_requests_get('http://foo.com/bar', html,
                             backlink='\n<a href="http://localhost/publish/faux"></a>')

    self.mox.ReplayAll()

    self.assert_success('')
    self.assert_success('', target='http://brid.gy/publish/faux')

  def test_bad_target_url(self):
    self.assert_error('Target must be brid.gy/publish/{facebook,twitter,instagram}',
                      target='foo')

  def test_unsupported_source_class(self):
    self.assert_error('Sorry, Google+ is not yet supported.',
                      target='http://brid.gy/publish/googleplus')

  def test_source_url_redirects(self):
    self.expect_requests_head('http://will/redirect', redirected_url='http://foo.com')

    self.expect_requests_get('http://foo.com', self.post_html % 'foo')
    self.mox.ReplayAll()
    # check that we include the original link, not the resolved one
    self.assert_success('foo - http://will/redirect', source='http://will/redirect')

  def test_source_url_redirects_with_refresh_header(self):
    self.mox.StubOutWithMock(requests, 'head', use_mock_anything=True)
    self.expect_requests_head('http://will/redirect',
                              response_headers={'refresh': '0; url=http://foo.com'})
    self.expect_requests_head('http://foo.com')

    self.expect_requests_get('http://foo.com', self.post_html % 'foo')
    self.mox.ReplayAll()
    # check that we include the original link, not the resolved one
    self.assert_success('foo - http://will/redirect', source='http://will/redirect')

  def test_bad_source(self):
    # no source
    self.source.key.delete()
    self.assert_error('Could not find <b>FakeSource</b> account for <b>foo.com</b>.')

    # source without publish feature
    self.source.features = ['listen']
    self.source.put()
    msg = 'Publish is not enabled'
    self.assert_error(msg)

    # status disabled
    self.source.features = ['publish']
    self.source.status = 'disabled'
    self.source.put()
    self.assert_error(msg)

    # two bad sources with same domain
    source_2 = self.source = testutil.FakeSource(id='z', **self.source.to_dict())
    source_2.status = 'enabled'
    source_2.features = ['listen']
    source_2.put()
    self.assert_error(msg)

    # one bad source, one good source, same domain. should automatically use the
    # good source.
    source_2.features.append('publish')
    source_2.put()
    self.expect_requests_get('http://foo.com/bar', self.post_html % 'xyz')
    self.mox.ReplayAll()
    self.assert_success('xyz - http://foo.com/bar')
    self.assertEquals(source_2.key, Publish.query().get().source)

  def test_source_with_multiple_domains(self):
    """Publish domain is second in source's domains list."""
    self.source.domains = ['baj.com', 'foo.com']
    self.source.domain_urls = ['http://baj.com/', 'http://foo.com/']
    self.source.put()
    self.expect_requests_get('http://foo.com/bar', self.post_html % 'xyz')
    self.mox.ReplayAll()
    self.assert_success('xyz - http://foo.com/bar')
    self.assertEquals(self.source.key, Publish.query().get().source)

  def test_source_missing_mf2(self):
    self.expect_requests_get('http://foo.com/bar', '')
    self.mox.ReplayAll()
    self.assert_error('No microformats2 data found in http://foo.com/')

    self.assertTrue(PublishedPage.get_by_id('http://foo.com/bar'))
    publish = Publish.query().get()
    self.assertEquals('failed', publish.status)
    self.assertEquals(self.source.key, publish.source)

  def test_no_content(self):
    self.expect_requests_get('http://foo.com/bar',
                             '<article class="h-entry h-as-note"></article>')
    self.mox.ReplayAll()

    self.assert_error('or no content was found')
    self.assertEquals('failed', Publish.query().get().status)

  def test_no_content_ignore_formatting(self):
    self.expect_requests_get('http://foo.com/bar',
                             '<article class="h-entry h-as-note"></article>')
    self.mox.ReplayAll()

    self.assert_error('or no content was found',
                      params={'bridgy_ignore_formatting': ''})
    self.assertEquals('failed', Publish.query().get().status)

  def test_multiple_items_chooses_first_that_works(self):
    html = ('<a class="h-card" href="http://mic.lim.com/">Mic Lim</a>\n' +
            self.post_html % 'foo')
    self.expect_requests_get('http://foo.com/bar', html)
    self.mox.ReplayAll()
    self.assert_success('foo - http://foo.com/bar')

  def test_type_not_implemented(self):
    self.expect_requests_get('http://foo.com/bar',
                             '<article class="h-entry h-as-like"></article>')
    self.mox.ReplayAll()

    # FakeSource.create() raises NotImplementedError on likes
    self.assert_error('Cannot publish likes')
    self.assertEquals('failed', Publish.query().get().status)

  def test_source_url_is_domain_url(self):
    self.source.put()
    self.assert_error("Looks like that's your home page.", source='https://foo.com#')

    # query params alone shouldn't trigger this
    self.expect_requests_get('http://foo.com/?p=123', self.post_html % 'foo')
    self.mox.ReplayAll()
    self.assert_success('foo - http://foo.com/?p=123',
                        source='http://foo.com/?p=123')

  def test_source_url_is_silo(self):
    self.source.put()
    self.assert_error(
      "Looks like that's a FakeSource URL. Try one from your web site instead!",
      source='http://fa.ke/post/123')
    self.assert_error(
      "Looks like that's a Facebook URL. Try one from your web site instead!",
      source='http://facebook.com/post/123')

  def test_embedded_type_not_implemented(self):
    self.expect_requests_get('http://foo.com/bar', """
<article class="h-entry">
  <div class="p-like-of">
    foo <a class="u-url" href="http://url">bar</a>
  </div>
</article>""")
    self.mox.ReplayAll()

    # FakeSource.create() returns an error message for verb='like'
    self.assert_error("Cannot publish likes")
    self.assertEquals('failed', Publish.query().get().status)

  def test_mf1_backward_compatibility_inside_hfeed(self):
    """This is based on Blogger's default markup, e.g.
    http://daisystanton.blogspot.com/2014/06/so-elections.html
    """
    self.expect_requests_get('http://foo.com/bar', """
<div class="blog-posts hfeed">
<div class="post hentry uncustomized-post-template">
<div class="post-body entry-content">
this is my article
</div></div></div>""")
    self.mox.ReplayAll()
    self.assert_success('this is my article - http://foo.com/bar')

  def test_ignore_hfeed_contents(self):
    """Background in https://github.com/snarfed/bridgy/issues/219"""
    self.expect_requests_get('http://foo.com/bar', """
<div class="blog-posts hfeed">
<div class="e-content">my feed</div>
<div class="h-entry">
<div class="e-content">my article</div>
</div>""")
    self.mox.ReplayAll()
    self.assert_success('my article - http://foo.com/bar')

  def test_tumblr_markup(self):
    """This is based on Tumblr's default markup, e.g.
    http://snarfed.tumblr.com/post/84623272717/stray-cat
    """
    self.expect_requests_get('http://foo.com/bar', """
<body>
<div id="content">
  <div class="post">
    <div class="copy"><p>this is my article</p></div>
    <div class="footer for_permalink"></div>
  </div>
</div>
</body>
""")
    self.mox.ReplayAll()
    self.assert_success('this is my article - http://foo.com/bar')

  def test_tumblr_markup_with_photo(self):
    """A tumblr post with a picture but no text.
    Based on http://require.aorcsik.com/post/98159554316/whitenoisegirl-the-clayprofessor-chris """
    self.expect_requests_get('http://foo.com/bar', """
<body>
<section id="content">
  <section class="post">
    <figure>
      <div class="photo-wrapper">
        <div class="photo-wrapper-inner">
          <a href="http://my/photo/download">
            <img src="http://my/photo/url">
          </a>
        </div>
      </div>
    </figure>
  </section>
</section>
</body>
""")
    self.mox.ReplayAll()
    self.assert_error('Could not find content')

  def test_returned_type_overrides(self):
    # FakeSource returns type 'post' when it sees 'rsvp'
    self.expect_requests_get('http://foo.com/bar', """
<article class="h-entry h-as-rsvp">
<p class="e-content">
<data class="p-rsvp" value="yes"></data>
<a class="u-in-reply-to" href="http://fa.ke/event"></a>
</p></article>""")
    self.mox.ReplayAll()
    self.assert_success('')
    self.assertEquals('post', Publish.query().get().type)

  def test_in_reply_to_domain_allows_subdomains(self):
    """(The code that handles this is in granary.Source.base_object.)"""
    subdomains = 'www.', 'mobile.', ''
    for i, subdomain in enumerate(subdomains):
      self.expect_requests_get('http://foo.com/%d' % i,
"""<div class="h-entry"><p class="e-content">
<a class="u-in-reply-to" href="http://%sfa.ke/a/b/d">foo</a>
</p></div>""" % subdomain)
    self.mox.ReplayAll()

    for i in range(len(subdomains)):
      resp = self.get_response(source='http://foo.com/%d' % i)
      self.assertEquals(200, resp.status_int, resp.body)

  def test_relative_u_url(self):
    """mf2py expands urls; this just check that we give it the source URL."""
    html = """<article class="h-entry">
<a class="u-url" href="/foo/bar"></a>
<p class="e-content">foo</p></article>"""
    self.expect_requests_get('http://foo.com/bar', html)
    self.mox.ReplayAll()
    self.assert_success('foo - http://foo.com/foo/bar')

  def test_all_errors_email(self):
    """Should send me email on *any* error from create() or preview_create()."""
    for i in range(2):
      self.expect_requests_get('http://foo.com/bar', self.post_html % 'foo')

    self.mox.StubOutWithMock(mail, 'send_mail')
    for subject in ('PublishHandler None failed: None (FakeSource)',
                    'PreviewHandler preview new: None (FakeSource)'):
      mail.send_mail(subject=subject, body=mox.IgnoreArg(),
                     sender=mox.IgnoreArg(), to=mox.IgnoreArg())

    self.mox.StubOutWithMock(self.source.gr_source, 'create',
                             use_mock_anything=True)
    self.source.gr_source.create(mox.IgnoreArg(), include_link=True
                                 ).AndRaise(exc.HTTPPaymentRequired('fooey'))

    self.mox.StubOutWithMock(self.source.gr_source, 'preview_create',
                             use_mock_anything=True)
    self.source.gr_source.preview_create(mox.IgnoreArg(), include_link=True
                                         ).AndRaise(Exception('bar'))

    self.mox.ReplayAll()
    self.assert_error('fooey', status=402)
    self.assertEquals(500, self.get_response(preview=True).status_int)

  def test_preview(self):
    html = self.post_html % 'foo'
    self.expect_requests_get('http://foo.com/bar', html)
    # make sure create() isn't called
    self.mox.StubOutWithMock(self.source.gr_source, 'create', use_mock_anything=True)
    self.mox.ReplayAll()
    self.assert_success('preview of foo', preview=True)

    publish = Publish.query().get()
    self.assertEquals(self.source.key, publish.source)
    self.assertEquals('complete', publish.status)
    self.assertEquals('preview', publish.type)
    self.assertEquals(html + self.backlink, publish.html)

  def test_bridgy_omit_link_query_param(self):
    self.expect_requests_get('http://foo.com/bar', self.post_html % 'foo')
    self.mox.ReplayAll()
    self.assert_success('foo', params={'bridgy_omit_link': 'True'})

  def test_bridgy_omit_link_mf2(self):
    html = """\
<article class="h-entry">
<div class="e-content">
foo<br /> <blockquote></blockquote>
</div>
<a class="u-bridgy-omit-link" href=""></a>
</article>"""
    self.expect_requests_get('http://foo.com/bar', html)
    self.mox.ReplayAll()
    self.assert_success('foo')

  def test_preview_omit_link_no_query_param_overrides_mf2(self):
    html = """\
<article class="h-entry">
<div class="e-content">foo</div>
</article>"""
    self.expect_requests_get('http://foo.com/bar', self.post_html % 'foo')
    self.mox.ReplayAll()

    resp = self.assert_success('preview of foo', preview=True)
    self.assertIn(
      '<input type="hidden" name="state" value="{&quot;bridgy_omit_link&quot;:true,',
      resp.body.decode('utf-8'))

  def test_preview_omit_link_query_param_overrides_mf2(self):
    html = """\
<article class="h-entry">
<div class="e-content">foo</div>
<a class="u-bridgy-omit-link" href=""></a>
</article>"""
    self.expect_requests_get('http://foo.com/bar', html)
    self.mox.ReplayAll()

    resp = self.assert_success('preview of foo - http://foo.com/bar',
                               preview=True,
                               params={'bridgy_omit_link': 'false'})
    self.assertIn(
      '<input type="hidden" name="state" value="{&quot;bridgy_omit_link&quot;:false,',
      resp.body.decode('utf-8'))

  def test_bridgy_ignore_formatting_query_param(self):
    self.expect_requests_get('http://foo.com/bar', """\
<article class="h-entry"><div class="e-content">
foo<br /> <blockquote>bar</blockquote>
</div></article>""")
    self.mox.ReplayAll()
    self.assert_success('foo bar', params={'bridgy_ignore_formatting': ''})

  def test_bridgy_ignore_formatting_mf2(self):
    self.expect_requests_get('http://foo.com/bar', """\
<article class="h-entry"><div class="e-content">
foo<br /> <blockquote>bar</blockquote>
<a class="u-bridgy-ignore-formatting" href=""></a>
</div></article>""")
    self.mox.ReplayAll()
    self.assert_success('foo bar')

  def test_expand_target_urls_u_syndication(self):
    """Comment on a post with a u-syndication value
    """
    self.mox.StubOutWithMock(self.source.gr_source, 'create',
                             use_mock_anything=True)

    self.expect_requests_get('http://foo.com/bar', """
    <article class="h-entry">
      <a class="u-url" href="http://foo.com/bar"></a>
      <a class="u-in-reply-to" href="http://orig.domain/baz">In reply to</a>
    </article>
    """)

    self.expect_requests_get('http://orig.domain/baz', """
    <article class="h-entry">
      <span class="p-name e-content">Original post</span>
      <a class="u-syndication" href="https://fa.ke/a/b">syndicated</a>
    </article>
    """)

    self.source.gr_source.create({
      'inReplyTo': [{'url': 'http://orig.domain/baz'},
                    {'url': 'https://fa.ke/a/b'}],
      'displayName': 'In reply to',
      'url': 'http://foo.com/bar',
      'objectType': 'comment',
    }, include_link=True).AndReturn(gr_source.creation_result({
      'url': 'http://fake/url',
      'id': 'http://fake/url',
      'content': 'This is a reply',
    }))

    self.mox.ReplayAll()
    self.assert_success('')

  def test_expand_target_urls_rel_syndication(self):
    """Publishing a like of a post with two rel=syndication values
    """

    self.mox.StubOutWithMock(self.source.gr_source, 'create',
                             use_mock_anything=True)

    self.expect_requests_get('http://foo.com/bar', """
    <article class="h-entry">
      <a class="u-url" href="http://foo.com/bar"></a>
      <a class="u-like-of" href="http://orig.domain/baz">liked this</a>
    </article>
    """)

    self.expect_requests_get('http://orig.domain/baz', """
    <link rel="syndication" href="https://fa.ke/a/b">
    <link rel="syndication" href="https://flic.kr/c/d">
    <article class="h-entry">
      <span class="p-name e-content">Original post</span>
    </article>
    """)

    self.source.gr_source.create({
      'verb': 'like',
      'displayName': 'liked this',
      'url': 'http://foo.com/bar',
      'object': [{'url': 'http://orig.domain/baz'},
                 {'url': 'https://fa.ke/a/b'},
                 {'url': 'https://flic.kr/c/d'}],
      'objectType': 'activity',
    }, include_link=True).AndReturn(gr_source.creation_result({
      'url': 'http://fake/url',
      'id': 'http://fake/url',
      'content': 'liked this',
    }))

    self.mox.ReplayAll()
    self.assert_success('')

  def test_expand_target_urls_h_cite(self):
    """Repost a post with a p-syndication h-cite value (syndication
    property is a dict rather than a string)
    """
    self.mox.StubOutWithMock(self.source.gr_source, 'create',
                             use_mock_anything=True)

    self.expect_requests_get('http://foo.com/bar', """
    <article class="h-entry">
      <a class="u-url" href="http://foo.com/bar"></a>
      <a class="u-repost-of" href="http://orig.domain/baz">reposted this</a>
    </article>
    """)

    self.expect_requests_get('http://orig.domain/baz', """
    <article class="h-entry">
      <span class="p-name e-content">Original post</span>
      <a class="p-syndication h-cite" href="https://fa.ke/a/b">On Fa.ke</a>
    </article>
    """)

    self.source.gr_source.create({
      'verb': 'share',
      'displayName': 'reposted this',
      'url': 'http://foo.com/bar',
      'object': [{'url': 'http://orig.domain/baz'},
                 {'url': 'https://fa.ke/a/b'}],
      'objectType': 'activity',
    }, include_link=True).AndReturn(gr_source.creation_result({
      'url': 'http://fake/url',
      'id': 'http://fake/url',
      'content': 'reposted this',
    }))

    self.mox.ReplayAll()
    self.assert_success('')

  def test_expand_target_urls_h_event_in_h_feed(self):
    """RSVP to an event is a single element inside an h-feed; we should handle
    it just like a normal post permalink page.
    """
    self.mox.StubOutWithMock(self.source.gr_source, 'create',
                             use_mock_anything=True)

    self.expect_requests_get('http://foo.com/bar', """
    <article class="h-entry">
      <a class="u-url" href="http://foo.com/bar"></a>
      <a class="u-in-reply-to" href="http://orig.domain/baz"></a>
      <span class="p-rsvp">yes</span>
    </article>
    """)

    self.expect_requests_get('http://orig.domain/baz', """
    <html class="h-feed">
      <article class="h-event">
        <span class="p-name e-content">Original post</span>
        <a class="u-syndication" href="https://fa.ke/a/b">On Fa.ke</a>
      </article>
    </html>
    """)

    self.source.gr_source.create({
      'url': 'http://foo.com/bar',
      'verb': 'rsvp-yes',
      'displayName': 'yes',
      'object': [{'url': 'http://orig.domain/baz'},
                 {'url': 'https://fa.ke/a/b'}],
      'objectType': 'activity',
    }, include_link=True).AndReturn(gr_source.creation_result({
      'url': 'http://fake/url',
      'id': 'http://fake/url',
      'content': 'RSVPd yes',
    }))

    self.mox.ReplayAll()
    self.assert_success('')

  def test_expand_target_urls_fetch_failure(self):
    """Fetching the in-reply-to URL fails, but that shouldn't prevent us
    from publishing the post itself.
    """
    self.mox.StubOutWithMock(self.source.gr_source, 'create',
                             use_mock_anything=True)

    self.expect_requests_get('http://foo.com/bar', """
    <article class="h-entry">
      <a class="u-url" href="http://foo.com/bar"></a>
      <a class="u-in-reply-to" href="http://orig.domain/baz">In reply to</a>
    </article>
    """)

    self.expect_requests_get('http://orig.domain/baz', '', status_code=404)

    self.source.gr_source.create({
      'inReplyTo': [{'url': 'http://orig.domain/baz'}],
      'displayName': 'In reply to',
      'url': 'http://foo.com/bar',
      'objectType': 'comment',
    }, include_link=True).AndReturn(gr_source.creation_result({
      'url': 'http://fake/url',
      'id': 'http://fake/url',
      'content': 'This is a reply',
    }))

    self.mox.ReplayAll()
    self.assert_success('')

  def test_expand_target_urls_no_microformats(self):
    """Publishing a like of a post that has no microformats; should have no
    problems posting the like anyway.
    """
    self.mox.StubOutWithMock(self.source.gr_source, 'create',
                             use_mock_anything=True)

    self.expect_requests_get('http://foo.com/bar', """
    <article class="h-entry">
      <a class="u-url" href="http://foo.com/bar"></a>
      <a class="u-like-of" href="http://orig.domain/baz">liked this</a>
    </article>
    """)

    self.expect_requests_get('http://orig.domain/baz', """
    <article>
      A fantastically well-written article
    </article>
    """)

    self.source.gr_source.create({
      'verb': 'like',
      'displayName': 'liked this',
      'url': 'http://foo.com/bar',
      'object': [{'url': 'http://orig.domain/baz'}],
      'objectType': 'activity',
    }, include_link=True).AndReturn(gr_source.creation_result({
      'url': 'http://fake/url',
      'id': 'http://fake/url',
      'content': 'liked this',
    }))

    self.mox.ReplayAll()
    self.assert_success('')

  def test_expand_target_urls_blacklisted_target(self):
    """RSVP to a domain in the webmention blacklist should not trigger a fetch.
    """
    self.mox.StubOutWithMock(self.source.gr_source, 'create',
                             use_mock_anything=True)

    self.expect_requests_get('http://foo.com/bar', """
    <article class="h-entry h-as-rsvp">
     <div class="e-content">
      <span class="p-rsvp" value="yes">yes</span>
      <a class="u-in-reply-to" href="http://fa.ke/homebrew-website-club"></a>
     </div>
      <a class="u-url" href="http://foo.com/bar"></a>
    </article>
    """)

    self.source.gr_source.create({
      'url': 'http://foo.com/bar',
      'verb': 'rsvp-yes',
      'displayName': 'yes',
      'object': [{'url': 'http://fa.ke/homebrew-website-club'}],
      'objectType': 'activity',
      'content': 'yes',
    }, include_link=True).AndReturn(gr_source.creation_result({
      'url': 'http://fake/url',
      'id': 'http://fake/url',
      'content': 'RSVPd yes',
    }))

    self.mox.ReplayAll()
    self.assert_success('')

  def test_in_reply_to_no_target(self):
    """in-reply-to an original that does not syndicate to the silo should
    fail with a helpful error message. The error message is generated by
    granary.
    """
    self.expect_requests_get('http://foo.com/bar', """
    <article class="h-entry">
      <a class="u-url" href="http://foo.com/bar"></a>
      In reply to a post on <a class="u-in-reply-to" href="http://original.domain/baz">original</a>
      <div class="p-name e-content">
        Great post about an important subject
      </div>
    </article>
    """)

    self.expect_requests_get('http://original.domain/baz', """
    <article class="h-entry">
      <div class="p-name e-content">
        boop
      </div>
      <a class="u-syndication" href="http://not-fake/2014">syndicated here</a>
    </article>
    """)

    self.mox.ReplayAll()

    self.assert_error('no fa.ke url to reply to')

  def test_html2text(self):
    """Test that using html2text renders whitespace ok in publish content."""
    # based on https://snarfed.org/2014-01-15_homebrew-website-club-tonight
    for i in range(2):
      self.expect_requests_get('http://foo.com/bar', """\
    <article class="h-entry"><div class="e-content">
      <p class="h-event">
      <a class="u-url p-name" href="http://h.w/c">
        Homebrew Website Club</a>
      is <em>tonight</em>!
      <img class="shadow" src="/pour_over_coffee_stand.jpg" /></p>
      <time class="dt-start">6:30pm PST</time> at

      <a href="https://wiki.mozilla.org/SF">Mozilla SF</a> and
      <a href="https://twitter.com/esripdx">Esri Portland</a>.<br />Join us!
    </p></div></article>
    """)

    self.mox.ReplayAll()
    expected = """\
Homebrew Website Club is _tonight_!

6:30pm PST at Mozilla SF and Esri Portland.
Join us!"""

    self.assert_success(expected, preview=True)
    expected += ' - http://foo.com/bar'
    resp = self.assert_success(expected, preview=False)
    self.assertEquals(expected, json.loads(resp.body)['content'])

  def test_unicode(self):
    """Test that we pass through unicode chars correctly."""
    text = u'Démo pour les développeur. Je suis navrée de ce problème.'
    for i in range(2):
      self.expect_requests_get('http://foo.com/bar', self.post_html % text,
                               content_type='text/html; charset=utf-8')
    self.mox.ReplayAll()

    for preview in False, True:
      self.assert_success(text, preview=preview, params={'bridgy_omit_link': ''})

  def test_utf8_meta_tag(self):
    self._test_charset_in_meta_tag('utf-8')

  def test_iso8859_meta_tag(self):
    """https://github.com/snarfed/bridgy/issues/385"""
    self._test_charset_in_meta_tag('iso-8859-1')

  def _test_charset_in_meta_tag(self, charset):
    """Test that we support charset in meta tag as well as HTTP header."""
    text = u'Démo pour les développeur. Je suis navrée de ce problème.'

    resp = requests.Response()
    resp._content = (u"""
<html>
<head><meta charset="%s"></head>
<body><article class="h-entry"><p class="e-content">%s</p></article></body>
<a href="http://localhost/publish/fake"></a>
</html>
""" % (charset, text)).encode(charset)
    resp._text = "shouldn't use this! " + text
    resp.url = 'http://foo.com/bar'
    resp.status_code = 200
    requests.get(resp.url, timeout=appengine_config.HTTP_TIMEOUT,
                 headers=util.USER_AGENT_HEADER).AndReturn(resp)
    self.mox.ReplayAll()

    self.assert_success(text, params={'bridgy_omit_link': ''})

  def test_missing_backlink(self):
    # use super to avoid this class's override that adds backlink
    super(PublishTest, self).expect_requests_get('http://foo.com/bar',
                                                 self.post_html % 'foo')
    self.mox.ReplayAll()
    self.assert_error("Couldn't find link to http://localhost/publish/fake")

  def test_facebook_comment_and_like_disabled(self):
    self.source = facebook.FacebookPage(id='789', features=['publish'],
                                        domains=['mr.x'])
    self.source.put()

    self.expect_requests_get('http://mr.x/like', """
    <article class="h-entry">
      <a class="u-like-of" href="http://facebook.com/789/posts/456">liked this</a>
      <a href="http://localhost/publish/facebook"></a>
    </article>""")
    self.expect_requests_get('http://mr.x/comment', """
    <article class="h-entry">
      <a class="u-in-reply-to" href="http://facebook.com/789/posts/456">reply</a>
      <a href="http://localhost/publish/facebook"></a>
    </article>""")
    self.mox.ReplayAll()

    self.assert_error('Facebook comments and likes are no longer supported',
                      source='http://mr.x/like',
                      target='http://brid.gy/publish/facebook')
    self.assertEquals('failed', Publish.query().get().status)

    self.assert_error('Facebook comments and likes are no longer supported',
                      source='http://mr.x/comment',
                      target='http://brid.gy/publish/facebook',
                      preview=True)

  def test_require_like_of_repost_of(self):
    """We only trigger on like-of and repost-of, not like or repost."""
    for prop in 'like', 'repost':
      url = 'http://foo.com/%s' % prop
      self.expect_requests_get(url, """
      <article class="h-entry">
        <p class="e-content">foo</p>
        <a class="u-url" href="%s"></a>
        <a class="u-%s" href="http://a/like"></a>
      </article>
      """ % (url, prop))

    self.mox.ReplayAll()
    for prop in 'like', 'repost':
      url = 'http://foo.com/%s' % prop
      self.assert_success('foo - %s' % url, source=url)
