"""Assignment 3 behavior tests: use isolated databases, never the bundled demo."""
import html
import re
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app as mini


class Assignment3(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_db = mini.DATABASE
        mini.DATABASE = str(Path(self.temp.name) / 'test.sqlite')
        mini.app.config.update(TESTING=True, SECRET_KEY='assignment3-test-key')
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
                INSERT INTO users(id,username,profile,created_at) VALUES
                  (1,'admin','','2020-01-01 00:00:00'),(2,'newcomer','','2020-01-01 00:00:00'),
                  (3,'maker','','2020-01-01 00:00:00'),(4,'reader','','2020-01-01 00:00:00');
            ''')
        self.client = mini.app.test_client()
        self.client.get('/')
        self.login(2)

    def tearDown(self):
        mini.DATABASE = self.old_db
        self.temp.cleanup()

    def sql(self, query, args=()):
        with sqlite3.connect(mini.DATABASE) as db:
            return db.execute(query, args).fetchall()

    def login(self, uid):
        with self.client.session_transaction() as session:
            session.clear()
            session.update(user_id=uid, username={1:'admin',2:'newcomer',3:'maker',4:'reader'}[uid])

    def post(self, pid, text, uid=3, date='2026-10-01 10:00:00', anonymous=0):
        self.sql('INSERT INTO posts(id,user_id,content,created_at,is_anonymous) VALUES (?,?,?,?,?)',
                 (pid, uid, text, date, anonymous))

    def recommend(self, uid=2, following=False):
        with mini.app.app_context():
            return mini.recommend(uid, following)

    def token(self, response):
        return html.unescape(re.search(r'name="review_token" value="([^"]+)"',response.get_data(as_text=True))[1])

    def test_migration_keeps_old_content_and_defaults(self):
        self.post(1, 'Keep this post')
        for _ in range(2): self.client.get('/')
        self.assertEqual(self.sql('SELECT content,is_introduction,is_anonymous FROM posts'), [('Keep this post',0,0)])

    def test_cold_start_unique_five_newest_and_small_or_empty_corpus(self):
        self.assertEqual(self.recommend(), [])
        for pid in range(1,9): self.post(pid, 'A friendly discussion')
        self.assertEqual([p['id'] for p in self.recommend()], [8,7,6,5,4])
        self.assertEqual([p['id'] for p in self.recommend(None)], [8,7,6,5,4])
        self.sql('DELETE FROM posts WHERE id > 2')
        self.assertEqual(len(self.recommend()),2)

    def test_positive_reactions_learn_topic_not_only_author_or_recency(self):
        self.post(1,'Woodworking timber cabinet tools')
        for pid in range(2,7): self.post(pid,'Woodworking timber cabinet tools',uid=4)
        for pid in range(7,15): self.post(pid,'Football stadium league match',uid=3)
        self.assertEqual([p['id'] for p in self.recommend()], [14,13,12,11,10])
        for reaction in ['like','love','laugh']:
            self.sql('DELETE FROM reactions')
            self.sql('INSERT INTO reactions(user_id,post_id,reaction_type) VALUES (2,1,?)',(reaction,))
            self.assertEqual([p['id'] for p in self.recommend()], [6,5,4,3,2])
        # Removing the signal really returns to the cold-start behavior.
        self.sql('DELETE FROM reactions')
        self.assertEqual([p['id'] for p in self.recommend()], [14,13,12,11,10])

    def test_negative_and_ambiguous_reactions_do_not_teach_topic(self):
        self.post(1,'Woodworking cabinet tools')
        for pid in range(2,7): self.post(pid,'Woodworking cabinet tools',uid=4)
        for pid in range(7,12): self.post(pid,'Football stadium league')
        for reaction in ['angry','sad','wow']:
            self.sql('DELETE FROM reactions')
            self.sql('INSERT INTO reactions(user_id,post_id,reaction_type) VALUES (2,1,?)',(reaction,))
            self.assertEqual([p['id'] for p in self.recommend()], [11,10,9,8,7])

    def test_following_bonus_filter_and_unfollow(self):
        for pid in range(1,7): self.post(pid,'A friendly post',uid=3)
        for pid in range(7,13): self.post(pid,'Another friendly post',uid=4)
        self.assertEqual(self.recommend(following=True), [])
        self.sql('INSERT INTO follows VALUES (2,3)')
        self.assertEqual([p['id'] for p in self.recommend()], [6,5,4,3,2])
        self.assertTrue(all(p['user_id']==3 for p in self.recommend(following=True)))
        self.sql('DELETE FROM follows')
        self.assertEqual(self.recommend(following=True), [])
        self.assertEqual([p['id'] for p in self.recommend()], [12,11,10,9,8])

    def test_own_reacted_anonymous_and_removed_posts_excluded(self):
        self.post(1,'own',uid=2)
        self.post(2,'already seen')
        self.post(3,'anonymous author secret',anonymous=1)
        self.post(4,'tier1badword')
        self.post(5,'tier2badword')
        self.post(6,'Helpful public content')
        self.sql("INSERT INTO reactions(user_id,post_id,reaction_type) VALUES (2,2,'like')")
        self.sql('INSERT INTO follows VALUES (2,3)')
        self.assertEqual([p['id'] for p in self.recommend()], [6])
        self.assertEqual([p['id'] for p in self.recommend(following=True)], [6])

    def test_anonymous_positive_reaction_does_not_train_profile(self):
        self.post(1,'Woodworking cabinet tools',anonymous=1)
        for pid in range(2,7): self.post(pid,'Woodworking cabinet tools')
        for pid in range(7,12): self.post(pid,'Football stadium league')
        self.sql("INSERT INTO reactions(user_id,post_id,reaction_type) VALUES (2,1,'love')")
        self.assertEqual([p['id'] for p in self.recommend()], [11,10,9,8,7])

    def test_display_order_is_newest_first_after_selection_and_escaped(self):
        self.post(1,'Timber wood tools')
        self.post(2,'Timber wood tools',date='2026-10-02 10:00:00')
        self.post(3,'<script>alert(1)</script> Wood',date='2026-10-03 10:00:00')
        self.sql("INSERT INTO reactions(user_id,post_id,reaction_type) VALUES (2,1,'like')")
        self.assertEqual([p['id'] for p in self.recommend()], [3,2])
        text=self.client.get('/?sort=recommended&page=90').get_data(as_text=True)
        self.assertNotIn('Page 90',text)
        self.assertNotIn('<script>alert(1)</script>',text)
        self.assertIn('&lt;script&gt;',text)
        self.assertIn('Shares words with posts you liked',text)

    def test_introduction_create_discover_reply_and_delete(self):
        r=self.client.post('/posts/new',data={'content':'Hello, I enjoy woodworking.','is_introduction':'1'})
        self.assertEqual(r.status_code,302)
        pid=self.sql('SELECT id FROM posts')[0][0]
        self.assertEqual(self.sql('SELECT user_id,is_introduction,is_anonymous FROM posts'),[(2,1,0)])
        self.post(100,'Ordinary post')
        self.login(3)
        text=self.client.get('/?sort=introductions').get_data(as_text=True)
        self.assertIn('Hello, I enjoy woodworking.',text)
        self.assertNotIn('Ordinary post',text)
        self.assertIn('/u/newcomer',text)
        self.client.post(f'/posts/{pid}/comment',data={'content':'Welcome! What do you make?'})
        self.assertIn('Welcome! What do you make?',self.client.get(f'/posts/{pid}').get_data(as_text=True))
        # Another user cannot delete this introduction.
        self.client.post(f'/posts/{pid}/delete')
        self.assertEqual(len(self.sql('SELECT id FROM posts WHERE id=?',(pid,))),1)
        self.login(2)
        self.client.post(f'/posts/{pid}/delete')
        self.assertEqual(self.sql('SELECT id FROM comments WHERE post_id=?',(pid,)),[])
        self.assertNotIn('Hello, I enjoy woodworking.',self.client.get('/?sort=introductions').get_data(as_text=True))

    def test_intro_filters_following_and_excludes_anonymous_even_legacy_bad_data(self):
        self.post(1,'Public intro',uid=3)
        self.post(2,'Private author marker',uid=4,anonymous=1)
        self.sql('UPDATE posts SET is_introduction=1')
        self.sql('INSERT INTO follows VALUES (2,3)')
        text=self.client.get('/?sort=introductions&show=following').get_data(as_text=True)
        self.assertIn('Public intro',text)
        self.assertNotIn('Private author marker',text)
        self.assertNotIn('Private author marker',self.client.get('/?sort=introductions').get_data(as_text=True))

    def test_introduction_moderation_retains_label_and_signed_kind(self):
        data={'content':'tier3badword','is_introduction':'1'}
        r=self.client.post('/posts/new',data=data)
        self.assertEqual(self.sql('SELECT id FROM posts'),[])
        self.assertIn('name="is_introduction" value="1"',r.get_data(as_text=True))
        token=self.token(r)
        # A token approved for an intro cannot be reused for a normal risky post.
        changed=self.client.post('/posts/new',data={'content':'tier3badword','intent':'publish','review_token':token})
        self.assertEqual(changed.status_code,200)
        self.assertEqual(self.sql('SELECT id FROM posts'),[])
        self.client.post('/posts/new',data={**data,'intent':'publish','review_token':token})
        self.assertEqual(self.sql('SELECT content,is_introduction FROM posts'),[('tier3badword',1)])
        self.assertNotIn('tier3badword',self.client.get('/?sort=introductions').get_data(as_text=True))

    def test_reject_anonymous_introduction_and_blank_intro(self):
        for data in [{'content':'Hello','is_introduction':'1','is_anonymous':'1'},
                     {'content':'  ','is_introduction':'1'}]:
            self.client.post('/posts/new',data=data)
        self.assertEqual(self.sql('SELECT id FROM posts'),[])

    def test_sandbox_never_changes_posts_comments_risk_or_goal(self):
        self.post(1,'Public post')
        before={t:self.sql('SELECT * FROM '+t) for t in ['users','posts','comments','reactions','weekly_goals']}
        with mini.app.app_context(): risk=mini.user_risk_analysis(2)
        for kind in ['post','comment']:
            for content in ['tier1badword','tier3badword https://example.com','Friendly practice']:
                r=self.client.post('/sandbox',data={'kind':kind,'content':content,'intent':'publish'})
                self.assertEqual(r.status_code,200)
                self.assertEqual(r.headers['Cache-Control'],'no-store')
                self.assertIn('Practice '+kind+' preview',r.get_data(as_text=True))
        after={t:self.sql('SELECT * FROM '+t) for t in before}
        self.assertEqual(before,after)
        with mini.app.app_context(): self.assertEqual(mini.user_risk_analysis(2),risk)
        with self.client.session_transaction() as s:
            self.assertNotIn('content',s)
        self.assertNotIn('Friendly practice',self.client.get('/').get_data(as_text=True))

    def test_sandbox_clear_validation_and_escaping(self):
        r=self.client.post('/sandbox',data={'content':'<script>alert(1)</script>'})
        self.assertNotIn('<script>alert(1)</script>',r.get_data(as_text=True))
        self.assertIn('&lt;script&gt;',r.get_data(as_text=True))
        self.assertNotIn('alert(1)',self.client.get('/sandbox').get_data(as_text=True))
        for content in ['', 'x'*5001]:
            self.assertNotIn('id="practice-preview"',self.client.post('/sandbox',data={'content':content}).get_data(as_text=True))

    def test_guest_cannot_practice_or_publish_intro(self):
        self.client.get('/logout')
        for route in ['/sandbox','/posts/new']:
            r=self.client.post(route,data={'content':'Hello','is_introduction':'1'})
            self.assertEqual(r.status_code,302)
            self.assertIn('/login',r.location)
        self.assertEqual(self.sql('SELECT id FROM posts'),[])
        self.assertEqual(self.client.get('/?sort=introductions').status_code,200)


if __name__ == '__main__':
    unittest.main()
