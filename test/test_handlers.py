"""Unit tests for handlers.py.
"""

import json
import StringIO
import urllib2


import handlers
import models
import testutil


class HandlersTest(testutil.HandlerTest):

  def setUp(self):
    super(HandlersTest, self).setUp()
    self.source = testutil.FakeSource.new(self.handler)
    self.source.set_activities(
      [{'object': {
            'id': 'tag:fa.ke,2013:000',
            'url': 'http://fa.ke/000',
            'content': 'asdf http://other/link qwert',
            'author': {
              'id': 'tag:fa.ke,2013:author_id',
              'image': {'url': 'http://example.com/ryan/image'},
              },
            'upstreamDuplicates': ['http://or.ig/post'],
            }}])
    self.source.set_event({
      'object': {
        'id': 'tag:fa.ke,2013:123',
        'url': 'http://fa.ke/events/123',
        'content': 'Come to the next #Bridgy meetup http://other/link',
        'upstreamDuplicates': ['http://or.ig/event'],
      },
      'id': '123',
      'url': 'http://fa.ke/events/123',
    })
    self.source.put()

  def check_response(self, url_template, expected):
    # use an HTTPS request so that URL schemes are converted
    resp = handlers.application.get_response(
      url_template % self.source.key.string_id(), scheme='https')
    self.assertEqual(200, resp.status_int, resp.body)
    header_lines = len(handlers.TEMPLATE.template.splitlines()) - 2
    actual = '\n'.join(resp.body.splitlines()[header_lines:-1])
    self.assert_equals(expected, actual)

  def test_post_html(self):
    self.check_response('/post/fake/%s/000', """\
<article class="h-entry">
<span class="u-uid">tag:fa.ke,2013:000</span>

  <div class="h-card p-author">
    <a class="u-url" href="http://fa.ke/author_id"></a>
    <img class="u-photo" src="https://example.com/ryan/image" alt="" />
  </div>

<a class="u-url" href="http://fa.ke/000"></a>
  <div class="e-content p-name">

  asdf http://other/link qwert
  </div>

</article>
""")

  def test_post_json(self):
    resp = handlers.application.get_response(
      '/post/fake/%s/000?format=json' % self.source.key.string_id(), scheme='https')
    self.assertEqual(200, resp.status_int, resp.body)
    self.assert_equals({
        'type': ['h-entry'],
        'properties': {
          'uid': ['tag:fa.ke,2013:000'],
          'url': ['http://fa.ke/000'],
          'content': [{ 'html': 'asdf http://other/link qwert',
                        'value': 'asdf http://other/link qwert',
                        }],
          'author': [{
              'type': ['h-card'],
              'properties': {
                'uid': ['tag:fa.ke,2013:author_id'],
                'url': ['http://fa.ke/author_id'],
                'photo': ['https://example.com/ryan/image'],
                },
              }],
          },
        }, json.loads(resp.body))

  def test_bad_source_type(self):
    resp = handlers.application.get_response('/post/not_a_type/%s/000' %
                                             self.source.key.string_id())
    self.assertEqual(400, resp.status_int)

  def test_bad_user(self):
    resp = handlers.application.get_response('/post/fake/not_a_user/000')
    self.assertEqual(400, resp.status_int)

  def test_bad_format(self):
    resp = handlers.application.get_response('/post/fake/%s/000?format=asdf' %
                                             self.source.key.string_id())
    self.assertEqual(400, resp.status_int)

  def test_bad_id(self):
    for url in ('/post/fake/%s/x"1', '/comment/fake/%s/123/y(2',
                '/like/fake/%s/abc/z$3'):
      resp = handlers.application.get_response(url % self.source.key.string_id())
      self.assertEqual(404, resp.status_int)

  def test_author_uid_not_tag_uri(self):
    self.source.get_activities()[0]['object']['author']['id'] = 'not a tag uri'
    resp = handlers.application.get_response(
      '/post/fake/%s/000?format=json' % self.source.key.string_id())
    self.assertEqual(200, resp.status_int, resp.body)
    props = json.loads(resp.body)['properties']['author'][0]['properties']
    self.assert_equals(['not a tag uri'], props['uid'])
    self.assertNotIn('url', props)

  def test_ignore_unknown_query_params(self):
    resp = handlers.application.get_response('/post/fake/%s/000?target=x/y/z' %
                                             self.source.key.string_id())
    self.assertEqual(200, resp.status_int)

  def test_pass_through_source_errors(self):
    self.mox.StubOutWithMock(testutil.FakeSource, 'get_post')
    testutil.FakeSource.get_post('000').AndRaise(urllib2.HTTPError(
      'url', 410, 'Gone', {}, StringIO.StringIO('Gone baby gone')))
    self.mox.ReplayAll()

    resp = handlers.application.get_response('/post/fake/%s/000' %
                                             self.source.key.string_id())
    self.assertEqual(410, resp.status_int)
    self.assertEqual('text/plain', resp.headers['Content-Type'])
    self.assertEqual('FakeSource error:\nGone baby gone', resp.body)

  def test_comment(self):
    self.source.set_comment({
        'id': 'tag:fa.ke,2013:a1-b2.c3',  # test alphanumeric id (like G+)
        'content': 'qwert',
        'inReplyTo': [{'url': 'http://fa.ke/000'}],
        'author': {'image': {'url': 'http://example.com/ryan/image'}},
        })

    self.check_response('/comment/fake/%s/000/a1-b2.c3', """\
<article class="h-entry">
<span class="u-uid">tag:fa.ke,2013:a1-b2.c3</span>

  <div class="h-card p-author">

    <img class="u-photo" src="https://example.com/ryan/image" alt="" />
  </div>

  <div class="e-content p-name">

  qwert
  <a class="u-mention" href="http://other/link"></a>
  </div>

<a class="u-in-reply-to" href="http://fa.ke/000"></a>
<a class="u-in-reply-to" href="http://or.ig/post"></a>

</article>
""")

  def test_like(self):
    self.source.gr_source.set_like({
        'objectType': 'activity',
        'verb': 'like',
        'id': 'tag:fa.ke,2013:111',
        'object': {'url': 'http://example.com/original/post'},
        'author': {'image': {'url': 'http://example.com/ryan/image'}},
        })

    self.check_response('/like/fake/%s/000/111', """\
<article class="h-entry h-as-like">
<span class="u-uid">tag:fa.ke,2013:111</span>

  <div class="h-card p-author">

    <img class="u-photo" src="https://example.com/ryan/image" alt="" />
  </div>

  <div class="p-name">likes this.</div>
  <div class="e-content">

  <a class="u-mention" href="http://other/link"></a>
  </div>

  <a class="u-like u-like-of" href="http://example.com/original/post"></a>
  <a class="u-like u-like-of" href="http://or.ig/post"></a>

</article>
""")

  def test_repost_with_syndicated_post_and_mentions(self):
    self.source.get_activities()[0]['object']['content'] += ' http://another/mention'

    models.SyndicatedPost(
      parent=self.source.key,
      original='http://or.ig/post',
      syndication='http://example.com/original/post').put()

    # needed to make original_post_discovery use the SyndicatedPost
    self.source.domain_urls = ['http://unused']
    self.source.put()

    self.source.gr_source.set_share({
        'objectType': 'activity',
        'verb': 'share',
        'id': 'tag:fa.ke,2013:111',
        'object': {'url': 'http://example.com/original/post'},
        'author': {
          'id': 'tag:fa.ke,2013:reposter_id',
          'url': 'http://personal.domain/',
          'image': {'url': 'http://example.com/ryan/image'},
          },
        })

    self.check_response('/repost/fake/%s/000/111', """\
<article class="h-entry h-as-repost">
<span class="u-uid">tag:fa.ke,2013:111</span>

  <div class="h-card p-author">
    <a class="u-url" href="http://personal.domain/"></a>
    <a class="u-url" href="http://fa.ke/reposter_id"></a>
    <img class="u-photo" src="https://example.com/ryan/image" alt="" />
  </div>

  <div class="p-name">reposts this.</div>
  <div class="e-content">

  <a class="u-mention" href="http://another/mention"></a>
  <a class="u-mention" href="http://other/link"></a>
  </div>

  <a class="u-repost u-repost-of" href="http://example.com/original/post"></a>
  <a class="u-repost u-repost-of" href="http://or.ig/post"></a>

</article>
""")

  def test_rsvp(self):
    self.source.gr_source.set_rsvp({
        'objectType': 'activity',
        'verb': 'rsvp-no',
        'id': 'tag:fa.ke,2013:111',
        'object': {'url': 'http://example.com/event'},
        'author': {
          'id': 'tag:fa.ke,2013:rsvper_id',
          'url': 'http://fa.ke/rsvper_id',  # same URL as FakeSource.user_url()
          'image': {'url': 'http://example.com/ryan/image'},
          },
        })

    self.check_response('/rsvp/fake/%s/000/111', """\
<article class="h-entry h-as-rsvp">
<span class="u-uid">tag:fa.ke,2013:111</span>

  <div class="h-card p-author">
    <a class="u-url" href="http://fa.ke/rsvper_id"></a>
    <img class="u-photo" src="https://example.com/ryan/image" alt="" />
  </div>

  <div class="p-name"><data class="p-rsvp" value="no">is not attending.</data></div>
  <div class="e-content">

  <a class="u-mention" href="http://other/link"></a>
  </div>

  <a class="u-in-reply-to" href="http://or.ig/event"></a>
  <a class="u-in-reply-to" href="http://example.com/event"></a>

</article>
""")

  def test_invite(self):
    self.source.gr_source.set_rsvp({
      'id': 'tag:fa.ke,2013:111',
      'objectType': 'activity',
      'verb': 'invite',
      'url': 'http://fa.ke/event',
      'actor': {
        'displayName': 'Mrs. Host',
        'url': 'http://fa.ke/host',
      },
      'object': {
        'objectType': 'person',
        'displayName': 'Ms. Guest',
        'url': 'http://fa.ke/guest',
      },
    })

    self.check_response('/rsvp/fake/%s/000/111', """\
<article class="h-entry">
<span class="u-uid">tag:fa.ke,2013:111</span>

  <div class="h-card p-author">
    <div class="p-name"><a class="u-url" href="http://fa.ke/host">Mrs. Host</a></div>

  </div>

<div class="p-name"><a class="u-url" href="http://fa.ke/event">invited</a></div>
  <div class="e-content">
  <div class="h-card p-invitee">
    <div class="p-name"><a class="u-url" href="http://fa.ke/guest">Ms. Guest</a></div>

  </div>

  <a class="u-mention" href="http://other/link"></a>
  </div>

  <a class="u-in-reply-to" href="http://or.ig/event"></a>

</article>
""")

  def test_original_post_urls_follow_redirects(self):
    self.source.gr_source.set_share({
        'objectType': 'activity',
        'verb': 'share',
        })

    self.expect_requests_head('http://or.ig/post',
                              redirected_url='http://or.ig/post/redirect')
    self.expect_requests_head('http://other/link',
                              redirected_url='http://other/link/redirect')
    self.mox.ReplayAll()

    self.check_response('/repost/fake/%s/000/111', """\
<article class="h-entry h-as-repost">
<span class="u-uid"></span>

  <div class="p-name">reposts this.</div>
  <div class="e-content">

  <a class="u-mention" href="http://other/link"></a>
  <a class="u-mention" href="http://other/link/redirect"></a>
  </div>

  <a class="u-repost u-repost-of" href="http://or.ig/post"></a>
  <a class="u-repost u-repost-of" href="http://or.ig/post/redirect"></a>

</article>
""")

  def test_strip_utm_query_params(self):
    self.source.get_activities()[0]['object'].update({
        'content': 'asdf http://other/link?utm_source=x&utm_medium=y&a=b qwert',
        'upstreamDuplicates': ['http://or.ig/post?utm_campaign=123'],
        })
    self.source.gr_source.set_share({'objectType': 'activity', 'verb': 'share'})
    self.mox.ReplayAll()

    self.check_response('/repost/fake/%s/000/111', """\
<article class="h-entry h-as-repost">
<span class="u-uid"></span>

  <div class="p-name">reposts this.</div>
  <div class="e-content">

  <a class="u-mention" href="http://other/link?a=b"></a>
  </div>

  <a class="u-repost u-repost-of" href="http://or.ig/post"></a>

</article>
""")

  def test_dedupe_http_and_https(self):
    self.source.get_activities()[0]['object'].update({
      'content': 'X http://mention/only Y https://reply Z https://upstream '
                 'W http://all',
      'upstreamDuplicates': ['http://upstream/only',
                             'http://upstream',
                             'http://all',
                           ],
      })

    self.source.set_comment({
      'inReplyTo': [{'url': 'https://reply/only'},
                    {'url': 'http://reply'},
                    {'url': 'https://all'},
                  ],
    })
    self.mox.ReplayAll()

    self.check_response('/comment/fake/%s/000/111', """\
<article class="h-entry">
<span class="u-uid"></span>

  <div class="e-content p-name">

  <a class="u-mention" href="http://mention/only"></a>
  <a class="u-mention" href="http://all"></a>
  </div>

  <a class="u-in-reply-to" href="https://reply/only"></a>
  <a class="u-in-reply-to" href="http://reply"></a>
  <a class="u-in-reply-to" href="https://all"></a>
  <a class="u-in-reply-to" href="http://upstream/only"></a>
  <a class="u-in-reply-to" href="http://upstream"></a>
  <a class="u-in-reply-to" href="http://all"></a>
  <a class="u-in-reply-to" href="https://reply"></a>
  <a class="u-in-reply-to" href="https://upstream"></a>

</article>
""")
