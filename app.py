from flask import Flask, render_template, request, redirect, url_for, session, flash, g, abort
from werkzeug.security import generate_password_hash, check_password_hash
from cryptography.fernet import Fernet
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from pathlib import Path
from jinja2 import select_autoescape
import os
import secrets
import collections
import json
import sqlite3
import hashlib
import re
import math
from datetime import datetime, timedelta, timezone

app = Flask(__name__)
# Flask does not autoescape .j2 filenames by default. Treat all Jinja HTML
# templates as HTML so user text stays text, including the private editor.
app.jinja_env.autoescape = select_autoescape(['html', 'htm', 'xml', 'j2'])
# Never ship a public signing key for sessions and review approvals.
# A restart logs out users unless a persistent environment key is supplied.
app.secret_key = os.environ.get('MINISOCIAL_SECRET_KEY') or secrets.token_hex(32)
BASE_DIR = Path(__file__).resolve().parent
DATABASE = str(BASE_DIR / 'database.sqlite')

# Load censorship data
# WARNING! The censorship.dat file contains disturbing language when decrypted. 
# If you want to test whether moderation works, 
# you can trigger censorship using these words: 
# tier1badword, tier2badword, tier3badword
ENCRYPTED_FILE_PATH = BASE_DIR / 'censorship.dat'
fernet = Fernet('xpplx11wZUibz0E8tV8Z9mf-wwggzSrc21uQ17Qq2gg=')
with open(ENCRYPTED_FILE_PATH, 'rb') as encrypted_file:
    encrypted_data = encrypted_file.read()
decrypted_data = fernet.decrypt(encrypted_data)
MODERATION_CONFIG = json.loads(decrypted_data)
TIER1_WORDS = MODERATION_CONFIG['categories']['tier1_severe_violations']['words']
TIER2_PHRASES = MODERATION_CONFIG['categories']['tier2_spam_scams']['phrases']
TIER3_WORDS = MODERATION_CONFIG['categories']['tier3_mild_profanity']['words']

def get_db():
    """
    Connect to the application's configured database. The connection
    is unique for each request and will be reused if this is called
    again.
    """
    if 'db' not in g:
        g.db = sqlite3.connect(
            DATABASE,
            detect_types=sqlite3.PARSE_DECLTYPES
        )
        g.db.row_factory = sqlite3.Row

    return g.db


# Coding Assignment 1: anonymous discussions (Chapter 3, Design Claim 12).
@app.before_request
def ensure_anonymous_posts_column():
    """Upgrade the existing database without deleting its posts or comments."""
    db = get_db()
    # The transaction also prevents concurrent requests from adding it twice.
    with db:
        db.execute('BEGIN IMMEDIATE')
        columns = {row['name'] for row in db.execute('PRAGMA table_info(posts)')}
        if 'is_anonymous' not in columns:
            db.execute(
                'ALTER TABLE posts ADD COLUMN is_anonymous INTEGER NOT NULL DEFAULT 0'
            )
        # Assignment 3, Chapter 5 DC 19: label introductions without changing old posts.
        if 'is_introduction' not in columns:
            db.execute('ALTER TABLE posts ADD COLUMN is_introduction INTEGER NOT NULL DEFAULT 0')
        # Store each member's chosen target separately for each calendar week.
        db.execute("""
            CREATE TABLE IF NOT EXISTS weekly_goals (
                user_id INTEGER NOT NULL,
                week_start TEXT NOT NULL,
                target INTEGER NOT NULL CHECK(target BETWEEN 1 AND 50),
                PRIMARY KEY (user_id, week_start),
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        """)


@app.teardown_appcontext
def close_connection(exception):
    """Closes the database again at the end of the request."""
    db = g.pop('db', None)

    if db is not None:
        db.close()


def query_db(query, args=(), one=False, commit=False):
    """
    Queries the database and returns a list of dictionaries, a single
    dictionary, or None. Also handles write operations.
    """
    db = get_db()
    
    # Using 'with' on a connection object implicitly handles transactions.
    # The 'with' statement will automatically commit if successful, 
    # or rollback if an exception occurs. This is safer.
    try:
        with db:
            cur = db.execute(query, args)
        
        # For SELECT statements, fetch the results after the transaction block
        if not commit:
            rv = cur.fetchall()
            return (rv[0] if rv else None) if one else rv
        
        # For write operations, we might want the cursor to get info like lastrowid
        return cur

    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return None

@app.template_filter('datetimeformat')
def datetimeformat(value):
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        dt = datetime.strptime(value, '%Y-%m-%d %H:%M:%S')
    else:
        return "N/A"
    return dt.strftime('%b %d, %Y %H:%M')

REACTION_EMOJIS = {
    'like': '❤️', 'love': '😍', 'laugh': '😂',
    'wow': '😮', 'sad': '😢', 'angry': '😠',
}
REACTION_TYPES = list(REACTION_EMOJIS.keys())


@app.route('/')
def feed():
    #  1. Get Pagination and Filter Parameters 
    try:
        page = int(request.args.get('page', 1))
    except ValueError:
        page = 1
    page = max(1, page)
    sort = request.args.get('sort', 'new').lower()
    show = request.args.get('show', 'all').lower()
    if sort not in {'new', 'popular', 'recommended', 'unanswered', 'introductions'}:
        sort = 'new'
    if show not in {'all', 'following'} or not session.get('user_id'):
        show = 'all'
    if sort == 'recommended':
        page = 1  # One selection of five, not a paginated copy of the same results.
    
    # Define how many posts to show per page
    POSTS_PER_PAGE = 10
    offset = (page - 1) * POSTS_PER_PAGE

    current_user_id = session.get('user_id')
    params = []

    #  2. Build the Query 
    where_clause = ""
    if show == 'following' and current_user_id:
        where_clause = "WHERE p.is_anonymous = 0 AND p.user_id IN (SELECT followed_id FROM follows WHERE follower_id = ?)"
        params.append(current_user_id)

    # Add the pagination parameters to the query arguments
    pagination_params = (POSTS_PER_PAGE, offset)

        # Show other users' posts that have no comments.
    if sort == 'unanswered':
        condition = (
            "NOT EXISTS "
            "(SELECT 1 FROM comments c WHERE c.post_id = p.id)"
        )

        if where_clause:
            where_clause += " AND " + condition
        else:
            where_clause = "WHERE " + condition

        # Exclude the logged-in user's own posts.
        if current_user_id:
            where_clause += " AND p.user_id != ?"
            params.append(current_user_id)

    if sort == 'introductions':
        condition = 'p.is_introduction = 1 AND p.is_anonymous = 0'
        where_clause += (' AND ' if where_clause else 'WHERE ') + condition

    unanswered_count = 0
    if sort == 'unanswered':
        count_row = query_db(
            f"""
            SELECT COUNT(*) AS total
            FROM posts p
            JOIN users u ON p.user_id = u.id
            {where_clause}
            """,
            params,
            one=True
        )
        unanswered_count = count_row['total']

    if sort == 'popular':
        query = f"""
            SELECT p.id, p.content, p.created_at, p.is_anonymous, p.is_introduction, u.username, u.id as user_id,
                   IFNULL(r.total_reactions, 0) as total_reactions
            FROM posts p
            JOIN users u ON p.user_id = u.id
            LEFT JOIN (
                SELECT post_id, COUNT(*) as total_reactions FROM reactions GROUP BY post_id
            ) r ON p.id = r.post_id
            {where_clause}
            ORDER BY total_reactions DESC, p.created_at DESC
            LIMIT ? OFFSET ?
        """
        final_params = params + list(pagination_params)
        posts = query_db(query, final_params)
    elif sort == 'recommended':
        posts = recommend(current_user_id, show == 'following')
    else:  # Default sort is 'new'
        query = f"""
            SELECT p.id, p.content, p.created_at, p.is_anonymous, p.is_introduction, u.username, u.id as user_id
            FROM posts p
            JOIN users u ON p.user_id = u.id
            {where_clause}
            ORDER BY p.created_at DESC
            LIMIT ? OFFSET ?
        """
        final_params = params + list(pagination_params)
        posts = query_db(query, final_params)

    posts_data = []
    for post in posts:
        # Determine if the current user follows the poster
        followed_poster = False
        if current_user_id and not post['is_anonymous'] and post['user_id'] != current_user_id:
            follow_check = query_db(
                'SELECT 1 FROM follows WHERE follower_id = ? AND followed_id = ?',
                (current_user_id, post['user_id']),
                one=True
            )
            if follow_check:
                followed_poster = True

        # Determine if the current user reacted to this post and with what reaction
        user_reaction = None
        if current_user_id:
            reaction_check = query_db(
                'SELECT reaction_type FROM reactions WHERE user_id = ? AND post_id = ?',
                (current_user_id, post['id']),
                one=True
            )
            if reaction_check:
                user_reaction = reaction_check['reaction_type']

        reactions = query_db('SELECT reaction_type, COUNT(*) as count FROM reactions WHERE post_id = ? GROUP BY reaction_type', (post['id'],))
        comments_raw = query_db('SELECT c.id, c.content, c.created_at, u.username, u.id as user_id FROM comments c JOIN users u ON c.user_id = u.id WHERE c.post_id = ? ORDER BY c.created_at ASC', (post['id'],))
        post_dict = dict(post)
        post_dict['content'], _ = moderate_content(post_dict['content'])
        comments_moderated = []
        for comment in comments_raw:
            comment_dict = dict(comment)
            comment_dict['content'], _ = moderate_content(comment_dict['content'])
            comments_moderated.append(comment_dict)
        posts_data.append({
            'post': post_dict,
            'reactions': reactions,
            'user_reaction': user_reaction,
            'followed_poster': followed_poster,
            'comments': comments_moderated
        })

    #  4. Render Template with Pagination Info 
    return render_template('feed.html.j2', 
                           posts=posts_data, 
                           current_sort=sort,
                           current_show=show,
                           unanswered_count=unanswered_count,
                           weekly_goal=weekly_goal_status(current_user_id),
                           page=page, # Pass current page number
                           per_page=POSTS_PER_PAGE, # Pass items per page
                           reaction_emojis=REACTION_EMOJIS,
                           reaction_types=REACTION_TYPES)

# Coding Assignment 1: specific contribution goals (Chapter 2, Design Claim 13).
def weekly_bounds(now=None):
    """Use UTC to match SQLite's CURRENT_TIMESTAMP on comments."""
    now = now or datetime.now(timezone.utc)
    monday = (now - timedelta(days=now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return monday, monday + timedelta(days=7)


def weekly_goal_status(user_id):
    """Count distinct other members' posts with a surviving reply this week."""
    if not user_id:
        return None
    start, end = weekly_bounds()
    week_key = start.strftime('%Y-%m-%d')
    goal = query_db(
        'SELECT target FROM weekly_goals WHERE user_id = ? AND week_start = ?',
        (user_id, week_key), one=True
    )
    # Multiple replies to the same post count once; own posts do not count.
    # Deleting the last qualifying reply also removes that post from progress.
    row = query_db("""
        SELECT COUNT(DISTINCT c.post_id) AS completed
        FROM comments c JOIN posts p ON p.id = c.post_id
        WHERE c.user_id = ? AND p.user_id != ?
          AND c.created_at >= ? AND c.created_at < ?
    """, (user_id, user_id, start.strftime('%Y-%m-%d %H:%M:%S'),
          end.strftime('%Y-%m-%d %H:%M:%S')), one=True)
    completed = row['completed']
    target = goal['target'] if goal else None
    return {
        'target': target,
        'completed': completed,
        'percent': min(100, int(completed * 100 / target)) if target else 0,
        'remaining': max(0, target - completed) if target else None,
        'week_label': start.strftime('%d.%m.') + ' - ' +
                      (end - timedelta(days=1)).strftime('%d.%m.%Y'),
    }


@app.route('/weekly-goal', methods=['POST'])
def set_weekly_goal():
    """Only the logged-in member can set their own current-week target."""
    user_id = session.get('user_id')
    if not user_id:
        flash('Log in to set your weekly goal.', 'warning')
        return redirect(url_for('login'))
    try:
        target = int(request.form.get('target', ''))
    except (TypeError, ValueError):
        target = 0
    if not 1 <= target <= 50:
        flash('Choose a whole-number target between 1 and 50.', 'warning')
        return redirect(url_for('feed'))
    start, _ = weekly_bounds()
    query_db("""
        INSERT INTO weekly_goals (user_id, week_start, target) VALUES (?, ?, ?)
        ON CONFLICT(user_id, week_start) DO UPDATE SET target = excluded.target
    """, (user_id, start.strftime('%Y-%m-%d'), target), commit=True)
    flash('Your weekly goal has been saved.', 'success')
    return redirect(url_for('feed'))


@app.route('/posts/new', methods=['POST'])
def add_post():
    """Handles creating a new post from the feed."""
    user_id = session.get('user_id')

    # Block access if user is not logged in
    if not user_id:
        flash('You must be logged in to create a post.', 'danger')
        return redirect(url_for('login'))

    content = request.form.get('content', '').strip()
    if not content:
        flash('Post cannot be empty.', 'warning')
        return redirect(url_for('feed'))
    is_anonymous = request.form.get('is_anonymous') == '1'
    # Introduction threads are deliberately public and named (Chapter 5 DC 19).
    is_introduction = request.form.get('is_introduction') == '1'
    if is_introduction and is_anonymous:
        flash('An introduction shows your name. Choose an introduction or an anonymous discussion.', 'warning')
        return redirect(url_for('feed', sort='introductions'))
    review = review_before_publish(content, 'post', is_anonymous=is_anonymous,
                                  is_introduction=is_introduction)
    if review is not None:
        return review

    # Keep the original for risk scoring/admin review. Public views filter it.
    db = get_db()
    cursor = db.execute(
        'INSERT INTO posts (user_id, content, is_anonymous, is_introduction) VALUES (?, ?, ?, ?)',
        (user_id, content, int(is_anonymous), int(is_introduction)))
    db.commit()
    flash('Your post was successfully created!', 'success')

    if is_introduction:
        return redirect(url_for('post_detail', post_id=cursor.lastrowid))

    # Redirect back to the main feed to see the new post
    return redirect(url_for('feed'))
    
    
@app.route('/posts/<int:post_id>/delete', methods=['POST'])
def delete_post(post_id):
    """Handles deleting a post."""
    user_id = session.get('user_id')

    # Block access if user is not logged in
    if not user_id:
        flash('You must be logged in to delete a post.', 'danger')
        return redirect(url_for('login'))

    # Find the post in the database
    post = query_db('SELECT id, user_id FROM posts WHERE id = ?', (post_id,), one=True)

    # Check if the post exists and if the current user is the owner
    if not post:
        flash('Post not found.', 'danger')
        return redirect(url_for('feed'))

    if post['user_id'] != user_id:
        # Security check: prevent users from deleting others' posts
        flash('You do not have permission to delete this post.', 'danger')
        return redirect(url_for('feed'))

    # If all checks pass, proceed with deletion
    db = get_db()
    # To maintain database integrity, delete associated records first
    db.execute('DELETE FROM comments WHERE post_id = ?', (post_id,))
    db.execute('DELETE FROM reactions WHERE post_id = ?', (post_id,))
    # Finally, delete the post itself
    db.execute('DELETE FROM posts WHERE id = ?', (post_id,))
    db.commit()

    flash('Your post was successfully deleted.', 'success')
    # Redirect back to the page the user came from, or the feed as a fallback
    return redirect(request.referrer or url_for('feed'))

@app.route('/u/<username>')
def user_profile(username):
    """Displays a user's profile page with moderated bio, posts, and latest comments."""
    
    user_raw = query_db('SELECT * FROM users WHERE username = ?', (username,), one=True)
    if not user_raw:
        abort(404)

    user = dict(user_raw)
    moderated_bio, _ = moderate_content(user.get('profile', ''))
    user['profile'] = moderated_bio

    # Do not associate anonymous discussions with anyone's public profile.
    posts_raw = query_db('SELECT id, content, user_id, created_at FROM posts WHERE user_id = ? AND is_anonymous = 0 ORDER BY created_at DESC', (user['id'],))
    posts = []
    for post_raw in posts_raw:
        post = dict(post_raw)
        moderated_post_content, _ = moderate_content(post['content'])
        post['content'] = moderated_post_content
        posts.append(post)

    comments_raw = query_db('SELECT c.id, c.content, c.user_id, c.post_id, c.created_at FROM comments c JOIN posts p ON c.post_id = p.id WHERE c.user_id = ? AND p.is_anonymous = 0 ORDER BY c.created_at DESC LIMIT 100', (user['id'],))
    comments = []
    for comment_raw in comments_raw:
        comment = dict(comment_raw)
        moderated_comment_content, _ = moderate_content(comment['content'])
        comment['content'] = moderated_comment_content
        comments.append(comment)

    followers_count = query_db('SELECT COUNT(*) as cnt FROM follows WHERE followed_id = ?', (user['id'],), one=True)['cnt']
    following_count = query_db('SELECT COUNT(*) as cnt FROM follows WHERE follower_id = ?', (user['id'],), one=True)['cnt']

    #  NEW: CHECK FOLLOW STATUS 
    is_currently_following = False # Default to False
    current_user_id = session.get('user_id')
    
    # We only need to check if a user is logged in
    if current_user_id:
        follow_relation = query_db(
            'SELECT 1 FROM follows WHERE follower_id = ? AND followed_id = ?',
            (current_user_id, user['id']),
            one=True
        )
        if follow_relation:
            is_currently_following = True
    # --

    return render_template('user_profile.html.j2', 
                           user=user, 
                           posts=posts, 
                           comments=comments,
                           followers_count=followers_count, 
                           following_count=following_count,
                           is_following=is_currently_following)
    

@app.route('/u/<username>/followers')
def user_followers(username):
    user = query_db('SELECT * FROM users WHERE username = ?', (username,), one=True)
    if not user:
        abort(404)
    followers = query_db('''
        SELECT u.username
        FROM follows f
        JOIN users u ON f.follower_id = u.id
        WHERE f.followed_id = ?
    ''', (user['id'],))
    return render_template('user_list.html.j2', user=user, users=followers, title="Followers of")

@app.route('/u/<username>/following')
def user_following(username):
    user = query_db('SELECT * FROM users WHERE username = ?', (username,), one=True)
    if not user:
        abort(404)
    following = query_db('''
        SELECT u.username
        FROM follows f
        JOIN users u ON f.followed_id = u.id
        WHERE f.follower_id = ?
    ''', (user['id'],))
    return render_template('user_list.html.j2', user=user, users=following, title="Users followed by")

@app.route('/posts/<int:post_id>')
def post_detail(post_id):
    """Displays a single post and its comments, with content moderation applied."""
    
    post_raw = query_db('''
        SELECT p.id, p.content, p.created_at, p.is_anonymous, p.is_introduction, u.username, u.id as user_id
        FROM posts p
        JOIN users u ON p.user_id = u.id
        WHERE p.id = ?
    ''', (post_id,), one=True)

    if not post_raw:
        # The abort function will stop the request and show a 404 Not Found page.
        abort(404)

    #  Moderation for the Main Post 
    # Convert the raw database row to a mutable dictionary
    post = dict(post_raw)
    # Unpack the tuple from moderate_content, we only need the moderated content string here
    moderated_post_content, _ = moderate_content(post['content'])
    post['content'] = moderated_post_content

    #  Fetch Reactions (No moderation needed) 
    reactions = query_db('''
        SELECT reaction_type, COUNT(*) as count
        FROM reactions
        WHERE post_id = ?
        GROUP BY reaction_type
    ''', (post_id,))

    #  Fetch and Moderate Comments 
    comments_raw = query_db('SELECT c.id, c.content, c.created_at, u.username, u.id as user_id FROM comments c JOIN users u ON c.user_id = u.id WHERE c.post_id = ? ORDER BY c.created_at ASC', (post_id,))
    
    comments = [] # Create a new list for the moderated comments
    for comment_raw in comments_raw:
        comment = dict(comment_raw) # Convert to a dictionary
        # Moderate the content of each comment
        moderated_comment_content, _ = moderate_content(comment['content'])
        comment['content'] = moderated_comment_content
        comments.append(comment)

    # Pass the moderated data to the template
    return render_template('post_detail.html.j2',
                           post=post,
                           reactions=reactions,
                           comments=comments,
                           reaction_emojis=REACTION_EMOJIS,
                           reaction_types=REACTION_TYPES)

@app.route('/about')
def about():
    return render_template('about.html.j2')

@app.route('/privacy')
def privacy():
    return render_template('privacy.html.j2')


@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        location = request.form.get('location', '')
        birthdate = request.form.get('birthdate', '')
        profile = request.form.get('profile', '')

        hashed_password = generate_password_hash(password)

        db = get_db()
        cur = db.cursor()
        try:
            cur.execute(
                'INSERT INTO users (username, password, location, birthdate, profile) VALUES (?, ?, ?, ?, ?)',
                (username, hashed_password, location, birthdate, profile)
            )
            db.commit()

            # 1. Get the ID of the user we just created.
            new_user_id = cur.lastrowid

            # 2. Add user info to the session cookie.
            session.clear() # Clear any old session data
            session['user_id'] = new_user_id
            session['username'] = username

            # 3. Flash a welcome message and redirect to the feed.
            flash(f'Welcome, {username}! Your account has been created.', 'success')
            return redirect(url_for('feed')) # Redirect to the main feed/dashboard

        except sqlite3.IntegrityError:
            flash('Username already taken. Please choose another one.', 'danger')
        finally:
            cur.close()
            db.close()
            
    return render_template('signup.html.j2')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']

        db = get_db()
        user = db.execute('SELECT * FROM users WHERE username = ?', (username,)).fetchone()
        db.close()

        # 1. Check if the user exists.
        # 2. If user exists, use check_password_hash to securely compare the password.
        #    This function handles the salt and prevents timing attacks.
        if user and check_password_hash(user['password'], password):
            # Password is correct!
            session['user_id'] = user['id']
            session['username'] = user['username']
            flash('Logged in successfully.', 'success')
            return redirect(url_for('feed'))
        else:
            # User does not exist or password was incorrect.
            flash('Invalid username or password.', 'danger')
            
    return render_template('login.html.j2')

@app.route('/logout')
def logout():
    session.clear()
    flash('Logged out.', 'info')
    return redirect(url_for('login'))

@app.route('/posts/<int:post_id>/comment', methods=['POST'])
def add_comment(post_id):
    """Handles adding a new comment to a specific post."""
    user_id = session.get('user_id')

    # Block access if user is not logged in
    if not user_id:
        flash('You must be logged in to comment.', 'danger')
        return redirect(url_for('login'))

    # A stale form must not create an orphan comment after a post was deleted.
    if query_db('SELECT id FROM posts WHERE id = ?', (post_id,), one=True) is None:
        abort(404)
    content = request.form.get('content', '').strip()
    if not content:
        flash('Comment cannot be empty.', 'warning')
        return redirect(url_for('post_detail', post_id=post_id))
    review = review_before_publish(content, 'comment', post_id=post_id)
    if review is not None:
        return review
    db = get_db()
    db.execute('INSERT INTO comments (post_id, user_id, content) VALUES (?, ?, ?)',
               (post_id, user_id, content))
    db.commit()
    flash('Your comment was added.', 'success')
    return redirect(url_for('post_detail', post_id=post_id))


@app.route('/comments/<int:comment_id>/delete', methods=['POST'])
def delete_comment(comment_id):
    """Handles deleting a comment."""
    user_id = session.get('user_id')

    # Block access if user is not logged in
    if not user_id:
        flash('You must be logged in to delete a comment.', 'danger')
        return redirect(url_for('login'))

    # Find the comment and the original post's author ID
    comment = query_db('''
        SELECT c.id, c.user_id, p.user_id as post_author_id
        FROM comments c
        JOIN posts p ON c.post_id = p.id
        WHERE c.id = ?
    ''', (comment_id,), one=True)

    # Check if the comment exists
    if not comment:
        flash('Comment not found.', 'danger')
        return redirect(request.referrer or url_for('feed'))

    # Security Check: Allow deletion if the user is the comment's author OR the post's author
    if user_id != comment['user_id'] and user_id != comment['post_author_id']:
        flash('You do not have permission to delete this comment.', 'danger')
        return redirect(request.referrer or url_for('feed'))

    # If all checks pass, proceed with deletion
    db = get_db()
    db.execute('DELETE FROM comments WHERE id = ?', (comment_id,))
    db.commit()

    flash('Comment successfully deleted.', 'success')
    # Redirect back to the page the user came from
    return redirect(request.referrer or url_for('feed'))

@app.route('/react', methods=['POST'])
def add_reaction():
    """Handles adding a new reaction or updating an existing one."""
    user_id = session.get('user_id')

    if not user_id:
        flash("You must be logged in to react.", "danger")
        return redirect(url_for('login'))

    post_id = request.form.get('post_id')
    new_reaction_type = request.form.get('reaction')

    if not post_id or not new_reaction_type:
        flash("Invalid reaction request.", "warning")
        return redirect(request.referrer or url_for('feed'))

    db = get_db()

    # Step 1: Check if a reaction from this user already exists on this post.
    existing_reaction = query_db('SELECT id FROM reactions WHERE post_id = ? AND user_id = ?',
                                 (post_id, user_id), one=True)

    if existing_reaction:
        # Step 2: If it exists, UPDATE the reaction_type.
        db.execute('UPDATE reactions SET reaction_type = ? WHERE id = ?',
                   (new_reaction_type, existing_reaction['id']))
    else:
        # Step 3: If it does not exist, INSERT a new reaction.
        db.execute('INSERT INTO reactions (post_id, user_id, reaction_type) VALUES (?, ?, ?)',
                   (post_id, user_id, new_reaction_type))

    db.commit()

    return redirect(request.referrer or url_for('feed'))

@app.route('/unreact', methods=['POST'])
def unreact():
    """Handles removing a user's reaction from a post."""
    user_id = session.get('user_id')

    if not user_id:
        flash("You must be logged in to unreact.", "danger")
        return redirect(url_for('login'))

    post_id = request.form.get('post_id')

    if not post_id:
        flash("Invalid unreact request.", "warning")
        return redirect(request.referrer or url_for('feed'))

    db = get_db()

    # Remove the reaction if it exists
    existing_reaction = query_db(
        'SELECT id FROM reactions WHERE post_id = ? AND user_id = ?',
        (post_id, user_id),
        one=True
    )

    if existing_reaction:
        db.execute('DELETE FROM reactions WHERE id = ?', (existing_reaction['id'],))
        db.commit()
        flash("Reaction removed.", "success")
    else:
        flash("No reaction to remove.", "info")

    return redirect(request.referrer or url_for('feed'))


@app.route('/u/<int:user_id>/follow', methods=['POST'])
def follow_user(user_id):
    """Handles the logic for the current user to follow another user."""
    follower_id = session.get('user_id')

    # Security: Ensure user is logged in
    if not follower_id:
        flash("You must be logged in to follow users.", "danger")
        return redirect(url_for('login'))

    # Security: Prevent users from following themselves
    if follower_id == user_id:
        flash("You cannot follow yourself.", "warning")
        return redirect(request.referrer or url_for('feed'))

    # Check if the user to be followed actually exists
    user_to_follow = query_db('SELECT id FROM users WHERE id = ?', (user_id,), one=True)
    if not user_to_follow:
        flash("The user you are trying to follow does not exist.", "danger")
        return redirect(request.referrer or url_for('feed'))
        
    db = get_db()
    try:
        # Insert the follow relationship. The PRIMARY KEY constraint will prevent duplicates if you've set one.
        db.execute('INSERT INTO follows (follower_id, followed_id) VALUES (?, ?)',
                   (follower_id, user_id))
        db.commit()
        username_to_follow = query_db('SELECT username FROM users WHERE id = ?', (user_id,), one=True)['username']
        flash(f"You are now following {username_to_follow}.", "success")
    except sqlite3.IntegrityError:
        flash("You are already following this user.", "info")

    return redirect(request.referrer or url_for('feed'))


@app.route('/u/<int:user_id>/unfollow', methods=['POST'])
def unfollow_user(user_id):
    """Handles the logic for the current user to unfollow another user."""
    follower_id = session.get('user_id')

    # Security: Ensure user is logged in
    if not follower_id:
        flash("You must be logged in to unfollow users.", "danger")
        return redirect(url_for('login'))

    db = get_db()
    cur = db.execute('DELETE FROM follows WHERE follower_id = ? AND followed_id = ?',
               (follower_id, user_id))
    db.commit()

    if cur.rowcount > 0:
        # cur.rowcount tells us if a row was actually deleted
        username_unfollowed = query_db('SELECT username FROM users WHERE id = ?', (user_id,), one=True)['username']
        flash(f"You have unfollowed {username_unfollowed}.", "success")
    else:
        # This case handles if someone tries to unfollow a user they weren't following
        flash("You were not following this user.", "info")

    # Redirect back to the page the user came from
    return redirect(request.referrer or url_for('feed'))

@app.route('/admin')
def admin_dashboard():
    """Displays the admin dashboard with users, posts, and comments, sorted by risk."""

    if session.get('username') != 'admin':
        flash("You do not have permission to access this page.", "danger")
        return redirect(url_for('feed'))

    RISK_LEVELS = { "HIGH": 5, "MEDIUM": 3, "LOW": 1 }
    PAGE_SIZE = 50

    def get_risk_profile(score):
        if score >= RISK_LEVELS["HIGH"]:
            return "HIGH", 3
        elif score >= RISK_LEVELS["MEDIUM"]:
            return "MEDIUM", 2
        elif score >= RISK_LEVELS["LOW"]:
            return "LOW", 1
        return "NONE", 0

    # Get pagination and current tab parameters
    try:
        users_page = int(request.args.get('users_page', 1))
        posts_page = int(request.args.get('posts_page', 1))
        comments_page = int(request.args.get('comments_page', 1))
    except ValueError:
        users_page = 1
        posts_page = 1
        comments_page = 1
    
    users_page, posts_page, comments_page = (max(1, page) for page in (users_page, posts_page, comments_page))
    current_tab = request.args.get('tab', 'users') # Default to 'users' tab

    users_offset = (users_page - 1) * PAGE_SIZE
    
    # First, get all users to calculate risk, then apply pagination in Python
    # It's more complex to do this efficiently in SQL if risk calc is Python-side
    all_users_raw = query_db('SELECT id, username, profile, created_at FROM users')
    all_users = []
    for user in all_users_raw:
        user_dict = dict(user)
        user_risk_score = user_risk_analysis(user_dict['id'])
        risk_label, risk_sort_key = get_risk_profile(user_risk_score)
        user_dict['risk_label'] = risk_label
        user_dict['risk_sort_key'] = risk_sort_key
        user_dict['risk_score'] = min(5.0, round(user_risk_score, 2))
        all_users.append(user_dict)

    all_users.sort(key=lambda x: x['risk_score'], reverse=True)
    total_users = len(all_users)
    users = all_users[users_offset : users_offset + PAGE_SIZE]
    total_users_pages = (total_users + PAGE_SIZE - 1) // PAGE_SIZE

    # --- Posts Tab Data ---
    posts_offset = (posts_page - 1) * PAGE_SIZE
    total_posts_count = query_db('SELECT COUNT(*) as count FROM posts', one=True)['count']
    total_posts_pages = (total_posts_count + PAGE_SIZE - 1) // PAGE_SIZE

    posts_raw = query_db(f'''
        SELECT p.id, p.content, p.created_at, u.username, u.created_at as user_created_at
        FROM posts p JOIN users u ON p.user_id = u.id
        ORDER BY p.id DESC
    ''')
    posts = []
    for post in posts_raw:
        post_dict = dict(post)
        final_score = content_risk_score(post_dict['content'], post_dict['user_created_at'])
        risk_label, risk_sort_key = get_risk_profile(final_score)
        post_dict['risk_label'] = risk_label
        post_dict['risk_sort_key'] = risk_sort_key
        post_dict['risk_score'] = round(final_score, 2)
        posts.append(post_dict)

    posts.sort(key=lambda x: x['risk_score'], reverse=True)
    posts = posts[posts_offset : posts_offset + PAGE_SIZE]

    # --- Comments Tab Data ---
    comments_offset = (comments_page - 1) * PAGE_SIZE
    total_comments_count = query_db('SELECT COUNT(*) as count FROM comments', one=True)['count']
    total_comments_pages = (total_comments_count + PAGE_SIZE - 1) // PAGE_SIZE

    comments_raw = query_db(f'''
        SELECT c.id, c.content, c.created_at, u.username, u.created_at as user_created_at
        FROM comments c JOIN users u ON c.user_id = u.id
        ORDER BY c.id DESC
    ''')
    comments = []
    for comment in comments_raw:
        comment_dict = dict(comment)
        score = content_risk_score(comment_dict['content'], comment_dict['user_created_at'])
        risk_label, risk_sort_key = get_risk_profile(score)
        comment_dict['risk_label'] = risk_label
        comment_dict['risk_sort_key'] = risk_sort_key
        comment_dict['risk_score'] = round(score, 2)
        comments.append(comment_dict)

    comments.sort(key=lambda x: x['risk_score'], reverse=True)
    comments = comments[comments_offset : comments_offset + PAGE_SIZE]


    return render_template('admin.html.j2', 
                           users=users, 
                           posts=posts, 
                           comments=comments,
                           
                           # Pagination for Users
                           users_page=users_page,
                           total_users_pages=total_users_pages,
                           users_has_next=(users_page < total_users_pages),
                           users_has_prev=(users_page > 1),

                           # Pagination for Posts
                           posts_page=posts_page,
                           total_posts_pages=total_posts_pages,
                           posts_has_next=(posts_page < total_posts_pages),
                           posts_has_prev=(posts_page > 1),

                           # Pagination for Comments
                           comments_page=comments_page,
                           total_comments_pages=total_comments_pages,
                           comments_has_next=(comments_page < total_comments_pages),
                           comments_has_prev=(comments_page > 1),

                           current_tab=current_tab,
                           PAGE_SIZE=PAGE_SIZE)



@app.route('/admin/delete/user/<int:user_id>', methods=['POST'])
def admin_delete_user(user_id):
    if session.get('username') != 'admin':
        flash("You do not have permission to perform this action.", "danger")
        return redirect(url_for('feed'))
        
    if user_id == session.get('user_id'):
        flash('You cannot delete your own account from the admin panel.', 'danger')
        return redirect(url_for('admin_dashboard'))
    
    db = get_db()
    db.execute('DELETE FROM users WHERE id = ?', (user_id,))
    db.commit()
    flash(f'User {user_id} and all their content has been deleted.', 'success')
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/delete/post/<int:post_id>', methods=['POST'])
def admin_delete_post(post_id):
    if session.get('username') != 'admin':
        flash("You do not have permission to perform this action.", "danger")
        return redirect(url_for('feed'))

    db = get_db()
    db.execute('DELETE FROM comments WHERE post_id = ?', (post_id,))
    db.execute('DELETE FROM reactions WHERE post_id = ?', (post_id,))
    db.execute('DELETE FROM posts WHERE id = ?', (post_id,))
    db.commit()
    flash(f'Post {post_id} has been deleted.', 'success')
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/delete/comment/<int:comment_id>', methods=['POST'])
def admin_delete_comment(comment_id):
    if session.get('username') != 'admin':
        flash("You do not have permission to perform this action.", "danger")
        return redirect(url_for('feed'))

    db = get_db()
    db.execute('DELETE FROM comments WHERE id = ?', (comment_id,))
    db.commit()
    flash(f'Comment {comment_id} has been deleted.', 'success')
    return redirect(url_for('admin_dashboard'))

@app.route('/rules')
def rules():
    return render_template('rules.html.j2')

@app.template_global()
def loop_color(user_id):
    # Generate a pastel color based on user_id hash
    h = hashlib.md5(str(user_id).encode()).hexdigest()
    r = int(h[0:2], 16)
    g = int(h[2:4], 16)
    b = int(h[4:6], 16)
    return f'rgb({r % 128 + 80}, {g % 128 + 80}, {b % 128 + 80})'


# Coding Assignment 2: moderation and risk analysis.
# The exact weights and precedence come from MiniSocial's /rules page.
def word_list_pattern(entries):
    """Match complete words/phrases, case-insensitively, never substrings.

    Escape the supplied rules so punctuation cannot become regex syntax.
    Longest alternatives come first; repeated words still count separately.
    Phrase whitespace may contain spaces, tabs or line breaks.
    """
    alternatives = [r"\s+".join(re.escape(part) for part in entry.split())
                    for entry in sorted(set(entries), key=len, reverse=True)
                    if entry.strip()]
    if not alternatives:
        return re.compile(r"(?!)")
    return re.compile(r"(?<!\w)(?:" + "|".join(alternatives) + r")(?!\w)", re.IGNORECASE)


TIER1_PATTERN = word_list_pattern(TIER1_WORDS)
TIER2_PATTERN = word_list_pattern(TIER2_PHRASES)
TIER3_PATTERN = word_list_pattern(TIER3_WORDS)
# Explicit URL formats supported by this demo; bare domains are not guessed.
URL_PATTERN = re.compile(r"(?<!\w)(?:https?://|ftp://|www\.)[^\s<>\"']+", re.IGNORECASE)


def analyse_content(content):
    """One shared rules engine for filtering, risk scores and private previews.

    Raw text remains in the database for administrator review and scoring.
    Every public content view calls moderate_content before rendering it.
    A preview is only an analysis: it writes no content or risk to the database.
    """
    text = content or ''
    if TIER1_PATTERN.search(text):
        return {'content': '[content removed due to severe violation]', 'score': 5.0,
                'reasons': ['Severe language is not allowed. The whole message will be removed.']}
    if TIER2_PATTERN.search(text):
        return {'content': '[content removed due to spam/scam policy]', 'score': 5.0,
                'reasons': ['A spam/scam phrase was detected. The whole message will be removed.']}

    # Tier 3 and URL points are per occurrence, not per distinct word or link.
    filtered, mild_count = TIER3_PATTERN.subn(lambda match: '*' * len(match.group()), text)

    def remove_link(match):
        # Preserve sentence punctuation following a link, including closing brackets.
        url = match.group()
        trimmed = url.rstrip('.,!?;:)]}')
        return '[link removed]' + url[len(trimmed):]

    filtered, link_count = URL_PATTERN.subn(remove_link, filtered)
    score = 2.0 * (mild_count + link_count)
    reasons = []
    if mild_count:
        reasons.append(f'{mild_count} mild-language match(es): use respectful wording. These words will be masked.')
    if link_count:
        reasons.append(f'{link_count} external link(s): links are removed under the community rules.')
    # Evaluate capitalization in the ORIGINAL text, before masking words/URLs.
    letters = [character for character in text if character.isalpha()]
    if len(letters) > 15 and sum(character.isupper() for character in letters) / len(letters) > 0.70:
        score += 0.5
        reasons.append('Excessive capitals: please use normal sentence case. Capitalization is not changed automatically.')
    return {'content': filtered, 'score': score, 'reasons': reasons}


def moderate_content(content):
    """Assignment 2.1: return (filtered text, uncapped content score)."""
    result = analyse_content(content)
    return result['content'], result['score']


def account_age_days(created_at, now=None):
    """Handle SQLite datetime values and ISO strings consistently in UTC.

    Legacy rows with no account date get no new-account multiplier.
    Future dates are treated as age zero rather than as negative ages.
    """
    if not created_at:
        return 30.0
    if isinstance(created_at, str):
        created_at = datetime.fromisoformat(created_at.replace('Z', '+00:00'))
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return max(0.0, (now - created_at).total_seconds() / 86400)


def user_risk_analysis(user_id):
    """Assignment 2.2: weighted content averages, account age, then cap at 5.

    Rule 2.2 explicitly specifies a weighted SUM of the three components;
    do not divide the result by five despite its 'weighted average' heading.
    Use original content scores, not age-adjusted post/comment scores, so
    the user's age multiplier is applied exactly once.
    """
    user = query_db('SELECT profile, created_at FROM users WHERE id = ?', (user_id,), one=True)
    if user is None:
        return 0.0
    profile_score = moderate_content(user['profile'])[1]
    posts = query_db('SELECT content FROM posts WHERE user_id = ?', (user_id,))
    comments = query_db('SELECT content FROM comments WHERE user_id = ?', (user_id,))
    average_posts = sum(moderate_content(row['content'])[1] for row in posts) / len(posts) if posts else 0.0
    average_comments = sum(moderate_content(row['content'])[1] for row in comments) / len(comments) if comments else 0.0
    content_score = profile_score + 3 * average_posts + average_comments
    age = account_age_days(user['created_at'])
    multiplier = 1.5 if age < 7 else 1.2 if age < 30 else 1.0
    return min(5.0, content_score * multiplier)


def content_risk_score(content, created_at):
    """Rule 2.1: only accounts younger than seven days get a 1.5 multiplier.

    Post/comment risk is not capped; only USER risk has a maximum of five.
    """
    return moderate_content(content)[1] * (1.5 if account_age_days(created_at) < 7 else 1.0)


def review_before_publish(content, kind, post_id=None, is_anonymous=False, is_introduction=False):
    """Chapter 4, Design Claim 20: a reminder at the moment of a violation.

    Review risky submissions before any INSERT. The member can edit/check
    again, cancel, or knowingly publish the filtered version. Signed approval
    covers this text, member, target post and anonymity setting, so editing a
    reviewed message cannot silently reuse approval of different content.
    No drafts or original text are stored in Flask's client-side session cookie.
    """
    result = analyse_content(content)
    payload = {'digest': hashlib.sha256(content.encode('utf-8')).hexdigest(),
               'user_id': session['user_id'], 'kind': kind, 'post_id': post_id,
               'is_anonymous': bool(is_anonymous), 'is_introduction': bool(is_introduction)}
    signer = URLSafeTimedSerializer(app.secret_key, salt='moderation-review-v1')
    approved = False
    if request.form.get('intent') == 'publish':
        try:
            approved = signer.loads(request.form.get('review_token', ''), max_age=1800) == payload
        except (BadSignature, SignatureExpired):
            pass  # Expired or modified approvals simply show a fresh review.
    if request.form.get('intent') != 'preview' and (result['score'] == 0 or approved):
        return None
    return render_template('moderation_review.html.j2', result=result, content=content,
                           kind=kind, post_id=post_id, is_anonymous=is_anonymous,
                           is_introduction=is_introduction,
                           review_token=signer.dumps(payload))


# Coding Assignment 3.1: a small content-based recommender with a follow bonus.
# Like/love/laugh are explicit positive signals. Wow is ambiguous; sad/angry
# are not evidence of liking a topic. No external AI service or library is used.
POSITIVE_REACTIONS = ('like', 'love', 'laugh')
RECOMMEND_STOP_WORDS = set("""a an and are as at be been being but by can could did do
 does doing for from had has have he her here him his how i if in into is it its
 just me more most my of on or our out she so some than that the their them then
 there these they this those to too up us very was we were what when where which
 who will with would you your new today really much about all also am get got
 not now one only own still such through time way weekend finally""".split())


def recommendation_terms(text):
    """Simple English word matching; hashtags retain their word, with no stemming."""
    return [word for word in re.findall(r"[^\W\d_]+", text.casefold())
            if len(word) > 2 and word not in RECOMMEND_STOP_WORDS]


def recommend(user_id, filter_following):
    """Select up to five relevant posts not already reacted to, then display newest first.

    TF-IDF makes rare words more useful than common words. Cosine similarity
    compares each post to the average of the member's positively reacted posts.
    Score = 2 * similarity + 1 if the author is followed. Scores select the
    best five; ties use date/id. A new member gets recent eligible posts.
    All existing reactions exclude their target from candidates. Following
    strictly limits candidates to followed authors. Fewer than five eligible
    posts returns fewer, never duplicates or posts outside that filter.

    Anonymous discussions are excluded from both training and recommendations
    so following/author relationships cannot reveal their writers. Moderation
    runs before word matching and the feed still moderates all displayed text.
    """
    rows = query_db("""
        SELECT p.id, p.content, p.created_at, p.is_anonymous, p.is_introduction,
               u.username, u.id AS user_id
        FROM posts p JOIN users u ON u.id = p.user_id
        WHERE p.is_anonymous = 0
        ORDER BY p.created_at DESC, p.id DESC
    """)
    documents = []
    for row in rows:
        filtered, _ = moderate_content(row['content'])
        if filtered in {'[content removed due to severe violation]',
                        '[content removed due to spam/scam policy]'}:
            continue
        post = dict(row)
        # Removed links are not a meaningful topic.
        words = recommendation_terms(filtered.replace('[link removed]', ''))
        documents.append((post, collections.Counter(words)))

    following = set()
    reacted = set()
    positive = set()
    if user_id:
        following = {r['followed_id'] for r in query_db(
            'SELECT followed_id FROM follows WHERE follower_id = ?', (user_id,))}
        for reaction in query_db('SELECT post_id, reaction_type FROM reactions WHERE user_id = ?', (user_id,)):
            reacted.add(reaction['post_id'])
            if reaction['reaction_type'] in POSITIVE_REACTIONS:
                positive.add(reaction['post_id'])
    if filter_following and not following:
        return []

    # Document frequencies count each word once per post, not once per occurrence.
    frequencies = collections.Counter(word for _, counts in documents for word in counts)
    idf = {word: math.log((1 + len(documents)) / (1 + count)) + 1
           for word, count in frequencies.items()}

    def vector(counts):
        weights = {word: (1 + math.log(count)) * idf[word] for word, count in counts.items()}
        norm = math.sqrt(sum(weight * weight for weight in weights.values()))
        return {word: weight / norm for word, weight in weights.items()} if norm else {}

    vectors = {post['id']: vector(counts) for post, counts in documents}
    profile = collections.Counter()
    for post, _ in documents:
        if post['id'] in positive and post['user_id'] != user_id:
            profile.update(vectors[post['id']])
    norm = math.sqrt(sum(weight * weight for weight in profile.values()))
    profile = {word: weight / norm for word, weight in profile.items()} if norm else {}

    candidates = []
    for post, _ in documents:
        if post['user_id'] == user_id or post['id'] in reacted:
            continue
        followed = post['user_id'] in following
        if filter_following and not followed:
            continue
        similarity = sum(weight * profile.get(word, 0) for word, weight in vectors[post['id']].items())
        score = 2 * similarity + int(followed)
        reasons = []
        if followed:
            reasons.append('From someone you follow')
        if similarity > 0:
            reasons.append('Shares words with posts you liked')
        post['recommendation_reason'] = ' · '.join(reasons) or 'Recent post to explore'
        # The numeric score stays internal; reasons explain the useful signals.
        candidates.append((score, post))

    def date_key(post):
        return (str(post['created_at']), post['id'])

    candidates.sort(key=lambda item: (item[0], *date_key(item[1])), reverse=True)
    selected = [post for _, post in candidates[:5]]
    return sorted(selected, key=date_key, reverse=True)


# Coding Assignment 3.3: Chapter 5, Design Claim 24, isolated practice.
@app.route('/sandbox', methods=['GET', 'POST'])
def sandbox():
    """Practise posts/comments without INSERTs, risk changes or public content.

    Draft text exists only in this response's form; it is never put in a
    database, URL, session cookie, log or shared application variable.
    There is no publish control: real publishing is a separate deliberate step.
    """
    if not session.get('user_id'):
        flash('Log in to use your private practice area.', 'info')
        return redirect(url_for('login'))
    content = ''
    kind = 'post'
    result = None
    if request.method == 'POST':
        content = request.form.get('content', '').strip()
        kind = 'comment' if request.form.get('kind') == 'comment' else 'post'
        if not content:
            flash('Write something to try the preview.', 'warning')
        elif len(content) > 5000:
            flash('Use at most 5000 characters in the practice area.', 'warning')
            content = content[:5000]
        else:
            result = analyse_content(content)
    response = app.make_response(render_template('sandbox.html.j2', content=content,
                                                  kind=kind, result=result))
    response.headers['Cache-Control'] = 'no-store'
    return response


if __name__ == '__main__':
    app.run(debug=True, port=8080)

