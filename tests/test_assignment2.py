"""Run with: python -m unittest discover -s tests -v

Tests use a temporary database. The bundled course database is never changed.
Only harmless tier1badword/tier2badword/tier3badword fixtures are used.
"""
import html
import re
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import app as mini


class ModerationRules(unittest.TestCase):
    def test_clean_and_empty(self):
        for value in ['', None, 'Hello everyone. Nice to meet you!']:
            self.assertEqual(mini.moderate_content(value), (value or '', 0.0))

    def test_severe_tier_precedence_and_fixed_score(self):
        self.assertEqual(mini.moderate_content('tier2badword TIER1BADWORD tier3badword https://example.com'),
                         ('[content removed due to severe violation]', 5.0))

    def test_spam_tier_fixed_score(self):
        self.assertEqual(mini.moderate_content('TIER2BADWORD tier3badword'),
                         ('[content removed due to spam/scam policy]', 5.0))

    def test_whole_words_case_and_repeated_occurrences(self):
        self.assertEqual(mini.moderate_content('tier3badword, Tier3BadWord!'), ('************, ************!', 4.0))
        self.assertEqual(mini.moderate_content('pretier1badword tier2badwords tier3badwording'),
                         ('pretier1badword tier2badwords tier3badwording', 0.0))

    def test_multiword_phrase_boundaries(self):
        pattern = mini.word_list_pattern(['sample spam phrase'])
        with patch.object(mini, 'TIER2_PATTERN', pattern):
            self.assertEqual(mini.moderate_content('A SAMPLE\nSPAM   PHRASE.')[1], 5.0)
            self.assertEqual(mini.moderate_content('sample spam phrases')[1], 0.0)

    def test_urls_count_separately_and_preserve_punctuation(self):
        self.assertEqual(mini.moderate_content('See https://example.com, www.example.org!'),
                         ('See [link removed], [link removed]!', 4.0))
        self.assertEqual(mini.moderate_content('Download FTP://example.org/file.'),
                         ('Download [link removed].', 2.0))

    def test_mixed_violations_are_added_and_content_not_capped(self):
        value = 'tier3badword tier3badword https://example.org'
        self.assertEqual(mini.moderate_content(value), ('************ ************ [link removed]', 6.0))

    def test_capitalization_strict_boundaries_and_original_text(self):
        self.assertEqual(mini.moderate_content('A' * 15)[1], 0.0)
        self.assertEqual(mini.moderate_content('A' * 16)[1], 0.5)
        self.assertEqual(mini.moderate_content('A' * 14 + 'a' * 6)[1], 0.0)  # Exactly 70%.
        self.assertEqual(mini.moderate_content('A' * 15 + 'a' * 5)[1], 0.5)
        self.assertEqual(mini.moderate_content('TIER3BADWORD AAAAAA')[1], 2.5)
        self.assertEqual(mini.moderate_content('THIS IS A LOUD MESSAGE')[0], 'THIS IS A LOUD MESSAGE')

    def test_age_boundaries_and_sqlite_strings(self):
        now = datetime(2026, 10, 21, tzinfo=timezone.utc)
        self.assertEqual(mini.account_age_days('2026-10-14 00:00:00', now), 7)
        self.assertEqual(mini.account_age_days('2026-10-21T03:00:00+03:00', now), 0)
        self.assertEqual(mini.account_age_days(now + timedelta(days=1), now), 0)
        self.assertEqual(mini.account_age_days(None, now), 30)
        for age, expected in [(6.999, 3), (7, 2), (29, 2), (30, 2)]:
            with self.subTest(age=age), patch.object(mini, 'account_age_days', return_value=age):
                self.assertEqual(mini.content_risk_score('tier3badword', now), expected)


class DatabaseWorkflow(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_db = mini.DATABASE
        mini.DATABASE = str(Path(self.temp.name) / 'test.sqlite')
        mini.app.config.update(TESTING=True, SECRET_KEY='test-key-only')
        with sqlite3.connect(mini.DATABASE) as db:
            db.executescript('''
                CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT, password TEXT,
                  profile TEXT, location TEXT, birthdate TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
                CREATE TABLE posts (id INTEGER PRIMARY KEY, user_id INTEGER, content TEXT,
                  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
                CREATE TABLE comments (id INTEGER PRIMARY KEY, user_id INTEGER, post_id INTEGER,
                  content TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
                CREATE TABLE reactions (id INTEGER PRIMARY KEY, user_id INTEGER, post_id INTEGER, reaction_type TEXT);
                CREATE TABLE follows (follower_id INTEGER, followed_id INTEGER);
            ''')
            for uid, username in [(1, 'admin'), (2, 'member'), (3, 'other')]:
                db.execute('INSERT INTO users(id, username, profile, password, created_at) VALUES (?, ?, ?, ?, ?)',
                           (uid, username, '', mini.generate_password_hash('test-password'), '2020-01-01 00:00:00'))
            db.execute("INSERT INTO posts(id,user_id,content) VALUES (1,3,'A friendly discussion')")
        self.client = mini.app.test_client()
        self.client.get('/')  # Migrate the old course schema.
        self.login_as(2)

    def tearDown(self):
        mini.DATABASE = self.old_db
        self.temp.cleanup()

    def login_as(self, uid):
        names = {1: 'admin', 2: 'member', 3: 'other'}
        with self.client.session_transaction() as session:
            session.clear()
            session['user_id'] = uid
            session['username'] = names[uid]

    def sql(self, query, args=()):
        with sqlite3.connect(mini.DATABASE) as db:
            return db.execute(query, args).fetchall()

    def count(self, table):
        return self.sql('SELECT COUNT(*) FROM ' + table)[0][0]

    def token(self, response):
        match = re.search(r'name="review_token" value="([^"]+)"', response.get_data(as_text=True))
        self.assertIsNotNone(match)
        return html.unescape(match[1])

    def publish(self, content, route='/posts/new', **extra):
        data = {'content': content, **extra}
        response = self.client.post(route, data=data)
        if response.status_code == 200:
            response = self.client.post(route, data={**data, 'intent': 'publish', 'review_token': self.token(response)})
        self.assertEqual(response.status_code, 302)
        return response

    def test_migration_preserves_existing_data_and_is_repeatable(self):
        for _ in range(2): self.assertEqual(self.client.get('/').status_code, 200)
        self.assertEqual(self.sql('SELECT content,is_anonymous FROM posts WHERE id=1'), [('A friendly discussion', 0)])

    def test_user_risk_empty_and_unknown_user(self):
        with mini.app.app_context():
            self.assertEqual(mini.user_risk_analysis(2), 0)
            self.assertEqual(mini.user_risk_analysis(9999), 0)

    def test_user_weighted_averages_age_and_cap(self):
        self.sql("UPDATE users SET profile='AAAAAAAAAAAAAAAA' WHERE id=2")  # 0.5
        self.sql("INSERT INTO posts(user_id,content) VALUES (2,'tier3badword'),(2,'Hello')")  # Mean 1
        self.sql("INSERT INTO comments(user_id,post_id,content) VALUES (2,1,'AAAAAAAAAAAAAAAA')")  # 0.5
        # Weighted sum: .5 + 3*1 + .5 = 4.0, with exactly one age multiplier.
        for age, expected in [(6.99, 5.0), (7, 4.8), (29.99, 4.8), (30, 4.0)]:
            with self.subTest(age=age), mini.app.app_context(), patch.object(mini, 'account_age_days', return_value=age):
                self.assertAlmostEqual(mini.user_risk_analysis(2), expected)

    def test_risky_post_is_not_saved_until_confirmed(self):
        response = self.client.post('/posts/new', data={'content': 'tier3badword'})
        self.assertEqual(self.count('posts'), 1)
        with mini.app.app_context(): self.assertEqual(mini.user_risk_analysis(2), 0)
        self.assertIn('************', response.get_data(as_text=True))
        response = self.client.post('/posts/new', data={'content': 'tier3badword', 'intent': 'publish', 'review_token': self.token(response)})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.count('posts'), 2)
        # Original is retained privately for risk scoring; public HTML is filtered.
        self.assertEqual(self.sql('SELECT content FROM posts WHERE user_id=2'), [('tier3badword',)])
        for route in ['/', '/posts/2', '/u/member']:
            page = self.client.get(route).get_data(as_text=True)
            self.assertNotIn('tier3badword', page)
            self.assertIn('************', page)

    def test_edit_and_recheck_then_publish_clean_text(self):
        first = self.client.post('/posts/new', data={'content': 'tier3badword'})
        second = self.client.post('/posts/new', data={'content': 'A polite replacement', 'intent': 'preview', 'review_token': self.token(first)})
        self.assertEqual(self.count('posts'), 1)
        self.assertIn('No rule violations detected', second.get_data(as_text=True))
        self.client.post('/posts/new', data={'content': 'A polite replacement', 'intent': 'publish', 'review_token': self.token(second)})
        with mini.app.app_context(): self.assertEqual(mini.user_risk_analysis(2), 0)
        self.assertEqual(self.count('posts'), 2)

    def test_cannot_bypass_review_with_changed_text_or_fake_token(self):
        first = self.client.post('/posts/new', data={'content': 'tier3badword'})
        for token in [self.token(first), 'fake']:
            response = self.client.post('/posts/new', data={'content': 'tier1badword', 'intent': 'publish', 'review_token': token})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(self.count('posts'), 1)

    def test_approval_bound_to_user_target_and_anonymity(self):
        first = self.client.post('/posts/new', data={'content': 'tier3badword'})
        token = self.token(first)
        self.client.post('/posts/new', data={'content': 'tier3badword', 'is_anonymous': '1', 'intent': 'publish', 'review_token': token})
        self.assertEqual(self.count('posts'), 1)
        self.login_as(3)
        self.client.post('/posts/new', data={'content': 'tier3badword', 'intent': 'publish', 'review_token': token})
        self.assertEqual(self.count('posts'), 1)
        self.client.post('/posts/1/comment', data={'content': 'tier3badword', 'intent': 'publish', 'review_token': token})
        self.assertEqual(self.count('comments'), 0)

    def test_expired_token_requires_review_again(self):
        from itsdangerous import TimestampSigner
        class OldSigner(TimestampSigner):
            def get_timestamp(self):
                return 1
        response = self.client.post('/posts/new', data={'content': 'tier3badword'})
        signer = mini.URLSafeTimedSerializer(mini.app.secret_key, salt='moderation-review-v1')
        payload = signer.loads(self.token(response))
        old_signer = mini.URLSafeTimedSerializer(mini.app.secret_key, salt='moderation-review-v1', signer=OldSigner)
        self.client.post('/posts/new', data={'content': 'tier3badword', 'intent': 'publish', 'review_token': old_signer.dumps(payload)})
        self.assertEqual(self.count('posts'), 1)

    def test_optional_preview_of_clean_content_does_not_publish(self):
        response = self.client.post('/posts/new', data={'content': 'Hello everyone', 'intent': 'preview'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.count('posts'), 1)

    def test_severe_and_spam_placeholders(self):
        for text, placeholder in [('tier1badword','severe violation'),('tier2badword','spam/scam policy')]:
            self.publish(text)
            post_id = self.sql('SELECT MAX(id) FROM posts')[0][0]
            page = self.client.get('/posts/' + str(post_id)).get_data(as_text=True)
            self.assertIn('[content removed due to ' + placeholder + ']', page)
            self.assertNotIn(text, page)

    def test_comments_moderated_and_orphans_rejected(self):
        response = self.client.post('/posts/1/comment', data={'content': 'tier3badword https://example.com'})
        self.assertEqual(self.count('comments'), 0)
        self.publish('tier3badword https://example.com', '/posts/1/comment')
        for route in ['/', '/posts/1', '/u/member']:
            page = self.client.get(route).get_data(as_text=True)
            self.assertNotIn('tier3badword', page)
            self.assertNotIn('https://example.com', page)
            self.assertIn('[link removed]', page)
        self.assertEqual(self.client.post('/posts/999/comment', data={'content':'Hello'}).status_code, 404)

    def test_signup_profile_is_filtered_and_scored(self):
        self.client.post('/signup', data={'username':'new-member','password':'test-password','profile':'tier3badword'})
        uid = self.sql("SELECT id FROM users WHERE username='new-member'")[0][0]
        page = self.client.get('/u/new-member').get_data(as_text=True)
        self.assertNotIn('tier3badword', page)
        self.assertIn('************', page)
        with mini.app.app_context(): self.assertEqual(mini.user_risk_analysis(uid), 3)

    def test_preview_escapes_html(self):
        response = self.client.post('/posts/new', data={'content':'</textarea><script>alert(1)</script>', 'intent':'preview'})
        page = response.get_data(as_text=True)
        self.assertNotIn('<script>alert(1)</script>', page)
        self.assertIn('&lt;script&gt;', page)

    def test_admin_access_and_labels(self):
        self.assertEqual(self.client.get('/admin').status_code, 302)
        self.login_as(1)
        for profile, label in [('Hello','NONE'), ('tier3badword','LOW'), ('tier3badword tier3badword','MEDIUM'), ('tier1badword','HIGH')]:
            self.sql('UPDATE users SET profile=? WHERE id=2',(profile,))
            page = self.client.get('/admin').get_data(as_text=True)
            self.assertIn('>' + label + '</span>', page)

    def test_old_high_risk_content_ranks_before_new_clean_rows(self):
        self.sql("UPDATE posts SET content='tier1badword' WHERE id=1")
        for _ in range(52): self.sql("INSERT INTO posts(user_id,content) VALUES (3,'New safe post')")
        self.sql("INSERT INTO comments(user_id,post_id,content) VALUES (3,1,'tier1badword')")
        for _ in range(52): self.sql("INSERT INTO comments(user_id,post_id,content) VALUES (3,1,'Safe reply')")
        self.login_as(1)
        page = self.client.get('/admin?tab=posts').get_data(as_text=True)
        self.assertIn('tier1badword', page)
        self.assertEqual(page.count('tier1badword'), 4)  # Title and text, posts AND comments.
        page2 = self.client.get('/admin?posts_page=2&comments_page=2').get_data(as_text=True)
        self.assertNotIn('tier1badword', page2)

    def test_assignment1_anonymity_unanswered_and_goals(self):
        self.client.post('/weekly-goal', data={'target':'5'})
        self.publish('tier3badword', is_anonymous='1')
        page = self.client.get('/posts/2').get_data(as_text=True)
        self.assertIn('Anonymous member',page)
        self.assertNotIn('************',self.client.get('/u/member').get_data(as_text=True))
        self.assertIn('A friendly discussion', self.client.get('/?sort=unanswered').get_data(as_text=True))
        self.publish('Useful reply', '/posts/1/comment')
        self.assertNotIn('A friendly discussion', self.client.get('/?sort=unanswered').get_data(as_text=True))
        with mini.app.app_context(): self.assertEqual(mini.weekly_goal_status(2)['completed'], 1)
        self.client.post('/comments/1/delete')
        self.assertIn('A friendly discussion', self.client.get('/?sort=unanswered').get_data(as_text=True))
        with mini.app.app_context(): self.assertEqual(mini.weekly_goal_status(2)['completed'], 0)

    def test_login_empty_content_and_logged_out_submission(self):
        self.client.get('/logout')
        self.assertEqual(self.client.post('/posts/new', data={'content':'Hello'}).status_code,302)
        self.assertEqual(self.count('posts'),1)
        self.client.post('/login',data={'username':'member','password':'test-password'})
        self.client.post('/posts/new',data={'content':'  '})
        self.client.post('/posts/1/comment',data={'content':'  '})
        self.assertEqual(self.count('posts'),1)
        self.assertEqual(self.count('comments'),0)


if __name__ == '__main__':
    unittest.main()
