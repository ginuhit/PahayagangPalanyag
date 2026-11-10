# engine.py
import os, json, re, time, datetime, hashlib, shutil, random, threading, subprocess, sys
from pathlib import Path
from urllib.parse import urljoin, quote
from bs4 import BeautifulSoup
from flask import Flask, render_template_string, request

import feedparser
import requests
import schedule
from PIL import Image, ImageDraw, ImageFont, ImageOps
import ollama

from curl_cffi import requests as cffi_requests 
from secrets import *

# ---------- CONFIGURATION ----------
BASE_DIR     = Path(__file__).parent
WEBSITE_DIR  = BASE_DIR / "website"
ARTICLES_DIR = WEBSITE_DIR / "articles"
DRAFTS_DIR   = BASE_DIR / "drafts"
ASSETS_DIR   = BASE_DIR / "assets"

SOURCES_FILE = BASE_DIR / "sources.json"
CONTEXT_FILE = BASE_DIR / "local_context.txt"
MEMORY_FILE  = BASE_DIR / "memory.json"

for d in (WEBSITE_DIR, ARTICLES_DIR, DRAFTS_DIR, ASSETS_DIR):
    d.mkdir(exist_ok=True)

MODEL_NAME   = "gemma4:12b"
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

# ---------- GLOBAL STATE & LOCKS ----------
publish_lock = threading.Lock()
CURRENT_DRAFT = {"data": None, "social_img": None, "web_img": None, "source_img": None, "status": "idle"}

# ---------- UNIFIED PUBLISHING (Fixes Issue 1 & 4) ----------
def publish_article(article_data, web_img_path, social_img_path):
    with publish_lock:
        if CURRENT_DRAFT.get("status") == "published":
            return
        
        print("\n[PUBLISH] Lock acquired. Starting publish sequence...")
        try:
            # 1. Publish to Facebook
            cat_slug = re.sub(r"[^a-z0-9]+", "-", article_data.get("category", "News").lower()).strip("-")
            slug = re.sub(r"[^a-z0-9]+", "-", article_data["headline"].lower()).strip("-")[:120]
            article_url = f"https://pahayagangpalanyag.pages.dev/articles/{cat_slug}/{slug}.html"
            
            print("[PUBLISH] Sending to Facebook...")
            publish_to_facebook(social_img_path, f"{article_data.get('social_caption', '')}\n\nRead here: {article_url}")
            
            # 2. Publish to Website
            print("[PUBLISH] Writing to Website...")
            publish_to_website(article_data, Path(web_img_path).name)
            update_homepage()
            
            # 3. Git Push (Safe Subprocess)
            print("[PUBLISH] Pushing to GitHub...")
            subprocess.run(["git", "add", "."], check=True, cwd=WEBSITE_DIR)
            subprocess.run(["git", "commit", "-m", "Auto-publish new article"], check=True, cwd=WEBSITE_DIR)
            subprocess.run(["git", "push"], check=True, cwd=WEBSITE_DIR)
            
            print("[PUBLISH] ✅ Done. Live on Facebook and Website.")
            CURRENT_DRAFT["status"] = "published"
            
        except subprocess.CalledProcessError as e:
            print(f"[GIT ERROR] Git command failed: {e}")
            CURRENT_DRAFT["status"] = "error"
        except Exception as e:
            print(f"[PUBLISH ERROR] {e}")
            CURRENT_DRAFT["status"] = "error"

# ---------- WEB SERVER (Flask) ----------
app = Flask(__name__)

@app.route('/', methods=['GET', 'POST'])
def dashboard():
    if not CURRENT_DRAFT["data"]:
        return "<div style='font-family:sans-serif; background:#111; color:#eee; height:100vh; padding:50px; text-align:center;'><h1>No pending drafts.</h1><p>The AI engine is running...</p><a href='/write' style='color:#1e5fd0; font-size:20px; text-decoration:none;'>✍️ Write a Custom Article</a></div>"
    
    if request.method == 'POST':
        action = request.form.get('action')
        
        CURRENT_DRAFT["data"]["headline"] = request.form.get('headline')
        CURRENT_DRAFT["data"]["byline"] = request.form.get('byline')
        CURRENT_DRAFT["data"]["editorial"] = request.form.get('editorial')
        
        if action == 'update':
            compose_image(
                CURRENT_DRAFT["source_img"], 
                CURRENT_DRAFT["data"]["headline"], 
                CURRENT_DRAFT["data"]["image_credit"], 
                CURRENT_DRAFT["social_img"], 
                CURRENT_DRAFT["web_img"]
            )
            return "<script>alert('Draft updated!'); window.location.href='/';</script>"
            
        elif action == 'approve':
            if CURRENT_DRAFT["status"] in ["approve", "published"]:
                return "<h1 style='color:white; background:#111; height:100vh; padding:50px;'>✅ Already approved. Check terminal for progress.</h1>"
            
            CURRENT_DRAFT["status"] = "approve"
            threading.Thread(target=publish_article, args=(
                CURRENT_DRAFT["data"], 
                CURRENT_DRAFT["web_img"], 
                CURRENT_DRAFT["social_img"]
            )).start()
            return "<h1 style='color:white; background:#111; height:100vh; padding:50px;'>✅ Approved! Publishing in the background. You can close this tab.</h1>"
            
        elif action == 'reject':
            CURRENT_DRAFT["status"] = "reject"
            return "<h1 style='color:white; background:#111; height:100vh; padding:50px;'>❌ Rejected.</h1>"

    html = """
    <html><head><style>
        body { font-family: sans-serif; background: #111; color: #eee; max-width: 1000px; margin: auto; padding: 20px; }
        img { max-width: 100%; border-radius: 8px; margin-bottom: 20px; }
        h1 { font-family: serif; border-bottom: 2px solid #333; padding-bottom: 10px; }
        .btn { padding: 15px 30px; font-size: 18px; margin: 10px; border: none; border-radius: 5px; cursor: pointer; color: white;}
        .approve { background: #2e7d32; }
        .reject { background: #c62828; }
        .update { background: #1e5fd0; }
        input[type="text"] { width: 100%; padding: 12px; margin-bottom: 15px; background: #222; color: white; border: 1px solid #444; border-radius: 5px; font-size: 16px; }
        textarea { width: 100%; height: 300px; padding: 12px; background: #222; color: white; border: 1px solid #444; border-radius: 5px; font-family: inherit; font-size: 16px; }
        label { font-weight: bold; color: #aaa; margin-bottom: 5px; display: block; margin-top: 15px; }
        .flex { display: flex; gap: 20px; }
        .flex > div { flex: 1; }
        .actions { text-align: center; margin-top: 30px; }
        .top-nav { display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; }
    </style></head><body>
        <div class="top-nav">
            <h1>Editor Dashboard</h1>
            <a href="/write" style="color:#1e5fd0; text-decoration:none; font-weight:bold;">✍️ Write Custom Article</a>
        </div>
        
        <div class="flex">
            <div><img src="/social_image"><p style="text-align:center; color:#888;">Facebook Version</p></div>
            <div><img src="/web_image"><p style="text-align:center; color:#888;">Website Version</p></div>
        </div>

        <form method="POST">
            <label>Headline</label>
            <input type="text" name="headline" value="{{ headline }}">
            
            <label>Byline</label>
            <input type="text" name="byline" value="{{ byline }}">

            <label>Editorial Text</label>
            <textarea name="editorial">{{ editorial }}</textarea>
            
            <div class="actions">
                <button type="submit" name="action" value="update" class="btn update">💾 Update Draft</button>
                <button type="submit" name="action" value="approve" class="btn approve">✅ Approve & Publish</button>
                <button type="submit" name="action" value="reject" class="btn reject">❌ Reject</button>
            </div>
        </form>
    </body></html>
    """
    return render_template_string(html, 
        headline=CURRENT_DRAFT["data"]["headline"], 
        byline=CURRENT_DRAFT["data"].get("byline", "Staff Writer"),
        editorial=CURRENT_DRAFT["data"]["editorial"])

@app.route('/write', methods=['GET', 'POST'])
def write_custom():
    if request.method == 'POST':
        headline = request.form.get('headline', 'Untitled')
        byline = request.form.get('byline', 'By the Editorial Board')
        category = request.form.get('category', 'Opinion')
        editorial = request.form.get('editorial', '')
        image_credit = request.form.get('image_credit', 'Pahayagang Palanyag')
        
        file = request.files.get('image')
        source_img_path = ASSETS_DIR / "custom_upload.jpg"
        if file and file.filename != '':
            file.save(source_img_path)
        else:
            Image.new("RGB", (1200, 630), (20, 40, 80)).save(source_img_path)

        social_img = DRAFTS_DIR / "draft_social.jpg"
        web_img = DRAFTS_DIR / "draft_web.jpg"
        compose_image(source_img_path, headline, image_credit, social_img, web_img)

        CURRENT_DRAFT["data"] = {
            "headline": headline, "byline": byline, "category": category,
            "editorial": editorial, "social_caption": f"{headline}\n\nRead more on Pahayagang Palanyag.",
            "image_credit": image_credit
        }
        CURRENT_DRAFT["source_img"] = source_img_path
        CURRENT_DRAFT["social_img"] = social_img
        CURRENT_DRAFT["web_img"] = web_img
        CURRENT_DRAFT["status"] = "idle"
        
        return "<script>alert('Draft loaded! Please review.'); window.location.href='/';</script>"

    return render_template_string("""
    <html><head><style>
        body { font-family: sans-serif; background: #111; color: #eee; max-width: 800px; margin: auto; padding: 40px; }
        h1 { font-family: serif; border-bottom: 2px solid #333; padding-bottom: 10px; }
        .btn { padding: 15px 30px; font-size: 18px; margin-top: 20px; border: none; border-radius: 5px; cursor: pointer; color: white; background: #1e5fd0; width: 100%; font-weight:bold;}
        input[type="text"], input[type="file"], textarea, select { width: 100%; padding: 12px; margin-bottom: 15px; background: #222; color: white; border: 1px solid #444; border-radius: 5px; font-size: 16px; }
        textarea { height: 300px; }
        label { font-weight: bold; color: #aaa; margin-bottom: 5px; display: block; margin-top: 15px; }
    </style></head><body>
        <h1>Write Custom Article</h1>
        <a href="/" style="color:#1e5fd0; text-decoration:none;">&larr; Back to Dashboard</a>
        <form method="POST" enctype="multipart/form-data">
            <label>Headline</label><input type="text" name="headline" required>
            <label>Byline</label><input type="text" name="byline" value="By the Editorial Board">
            <label>Category</label>
            <select name="category">
                <option value="News">News</option><option value="Politics">Politics</option><option value="Metro Manila">Metro Manila</option>
                <option value="Opinion">Opinion</option><option value="Sports">Sports</option><option value="Business">Business</option>
                <option value="Technology">Technology</option><option value="Entertainment">Entertainment</option>
            </select>
            <label>Image Credit</label><input type="text" name="image_credit" value="Pahayagang Palanyag">
            <label>Upload Cover Image</label><input type="file" name="image" accept="image/*" required>
            <label>Editorial Text</label><textarea name="editorial" required></textarea>
            <button type="submit" class="btn">Generate Draft</button>
        </form>
    </body></html>""")

@app.route('/social_image')
def serve_social(): return open(CURRENT_DRAFT["social_img"], "rb").read()
@app.route('/web_image')
def serve_web(): return open(CURRENT_DRAFT["web_img"], "rb").read()

def run_server():
    app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)

# ---------- AI WRITER ----------
SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string"}, "byline": {"type": "string"},
        "category": {"type": "string"}, "editorial": {"type": "string"},
        "social_caption": {"type": "string"}, "credibility": {"type": "number"},
        "image_credit": {"type": "string"},
    },
    "required": ["headline", "byline", "category", "editorial", "social_caption", "credibility", "image_credit"],
}

PROMPT_TEMPLATE = """You are the Editor-in-Chief of Pahayagang Palanyag, a premium, independent newsroom.
Your writing is analytical, factual, elegant, and thought-provoking.

### LOCAL CONTEXT
{context}

### SOURCE ARTICLE
Source: {source}
Title: {title}
Text:


### TASK
Write a 4-6 paragraph editorial (minimum 400 words). Do not give up halfway. Do not use clichés.
CRITICAL: If the story is not about Parañaque, do not force a local angle. Analyze the broader national or human impact.
Provide a category. You MUST choose EXACTLY one from this list:
- News
- Politics
- Metro Manila
- Opinion
- Sports
- Business
- Technology
- Entertainment

Write a 2-sentence social hook.
Provide a byline (e.g., "By the Editorial Board").

CRITICAL: You MUST provide an image credit. Look at the Source Article's domain or publisher name (e.g., "INQUIRER.net", "Philstar", "Rappler"). Do NOT output "None". Output the actual name of the news organization.
"""

def ai_write(article):
    full_text = get_full_article(article["link"]) or article.get("summary", "")
    context_str = load_context()
    prompt = PROMPT_TEMPLATE.format(context=context_str, source=article["source"], title=article["title"], text=full_text[:3500])
    
    resp = ollama.chat(
        model=MODEL_NAME, messages=[{"role": "user", "content": prompt}],
        format=SCHEMA, options={"temperature": 0.6, "num_predict": 8192, "num_ctx": 8192}
    )
    return json.loads(resp["message"]["content"])

# ---------- IMAGE COMPOSER (Fixes Issue 3) ----------
def compose_image(source_img_path, headline, credit, social_out_path, web_out_path):
    original_img = Image.open(source_img_path).convert("RGBA")
    logo_path = ASSETS_DIR / "logo.png"
    
    font_bold = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 40)
    font_small = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 16)

    # Sanitize Credit
    clean_credit = str(credit).strip() if credit else ""
    if clean_credit.lower() in ["none", "null", "n/a", ""]:
        clean_credit = "Photo via Source Article"
    credit_text = f"📷 {clean_credit}"

    def add_overlays(base_img, is_social=False):
        draw = ImageDraw.Draw(base_img, 'RGBA')
        
        # MASSIVE Logo Box
        if logo_path.exists():
            logo = Image.open(logo_path).convert("RGBA")
            box_size = 250 if is_social else 300
            logo.thumbnail((box_size - 30, box_size - 30))
            box_x1 = base_img.width - box_size - 20
            draw.rectangle([(box_x1, 0), (base_img.width - 20, box_size)], fill=(255, 255, 255, 255))
            base_img.paste(logo, (box_x1 + (box_size - logo.width)//2, (box_size - logo.height)//2), logo)

        # Credit Line
        draw.rectangle([(0, base_img.height - 40), (base_img.width, base_img.height)], fill=(0, 0, 0, 200))
        draw.text((20, base_img.height - 30), credit_text, font=font_small, fill=(220, 220, 220))

        # Lower Third (Facebook Only)
        if is_social:
            box_height = 180
            box_y_start = base_img.height - box_height
            
            draw.rectangle([(0, box_y_start), (base_img.width, base_img.height - 40)], fill=(30, 95, 208, 255))
            draw.rectangle([(0, box_y_start - 30), (350, box_y_start + 10)], fill=(10, 10, 10, 255))
            draw.text((20, box_y_start - 20), "PAHAYAGANG PALANYAG", font=font_small, fill=(255, 255, 255))
            
            words = headline.split()
            lines, current = [], ""
            for word in words:
                if font_bold.getlength(f"{current} {word}".strip()) < base_img.width - 100:
                    current = f"{current} {word}".strip()
                else:
                    lines.append(current); current = word
            if current: lines.append(current)
            
            y_text = box_y_start + 20
            for line in lines[:3]:
                draw.text((40, y_text), line, font=font_bold, fill=(255, 255, 255))
                y_text += 45

    web_img = original_img.copy()
    add_overlays(web_img, is_social=False)
    web_img.convert("RGB").save(web_out_path, quality=92)

    social_img = ImageOps.fit(original_img, (1200, 630), Image.Resampling.LANCZOS)
    add_overlays(social_img, is_social=True)
    social_img.convert("RGB").save(social_out_path, quality=92)

# ---------- PUBLISHING HELPERS ----------
def publish_to_facebook(image_path, caption):
    url = f"https://graph.facebook.com/v19.0/{FACEBOOK_PAGE_ID}/photos"
    with open(image_path, "rb") as f:
        r = requests.post(url, data={"message": caption, "access_token": FACEBOOK_PAGE_ACCESS_TOKEN}, files={"source": f})
    if r.status_code == 200:
        print(f"[FB] ✅ Successfully posted!")
    else:
        print(f"[FB] ❌ Error: {r.text}")

def publish_to_website(article_data, image_filename):
    cat_slug = re.sub(r"[^a-z0-9]+", "-", article_data.get("category", "News").lower()).strip("-")
    cat_dir = ARTICLES_DIR / cat_slug
    cat_dir.mkdir(exist_ok=True)
    
    art_slug = re.sub(r"[^a-z0-9]+", "-", article_data["headline"].lower()).strip("-")[:120]
    new_image_name = f"{art_slug}.jpg"
    paragraphs = ''.join(f'<p>{p}</p>' for p in article_data['editorial'].split(chr(10)) if p.strip())
    
    byline_text = article_data.get('byline', '').strip()
    if len(byline_text) < 3:
        byline_text = "By the Editorial Board"
    
    # Sanitize Credit for HTML
    clean_credit = str(article_data.get('image_credit', '')).strip()
    if clean_credit.lower() in ["none", "null", "n/a", ""]:
        clean_credit = "Photo via Source Article"

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8"><title>{article_data['headline']} | Pahayagang Palanyag</title>
    <link rel="icon" href="../../favicon.png"><link rel="stylesheet" href="../../style.css">
    <link href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@700;900&family=Inter:wght@400;600&display=swap" rel="stylesheet">
</head>
<body>
    <header class="site-header"><div class="container"><a href="../../index.html" class="header-brand"><img src="../../PPICON.png" class="header-icon"><img src="../../PPTITLE.png" class="header-text-img"></a></div></header>
    <nav class="site-nav"><a href="../../index.html">Home</a></nav>
    <main class="container">
        <div class="article-header">
            <h1 class="article-title">{article_data['headline']}</h1>
            <p class="byline">{byline_text} | {article_data.get('category', 'News')}</p>
        </div>
        <img src="{new_image_name}" class="hero-image-article">
        <div class="article-content">{paragraphs}</div>
        <p style="text-align:center; font-size:0.8rem; color:#888; margin-top:-40px; margin-bottom:40px;">{clean_credit}</p>
    </main>
</body>
</html>"""
    (cat_dir / f"{art_slug}.html").write_text(html)
    shutil.copy(DRAFTS_DIR / image_filename, cat_dir / new_image_name)

def update_homepage():
    # Force Sync Assets
    if (ASSETS_DIR / "PPICON.png").exists(): shutil.copy(ASSETS_DIR / "PPICON.png", WEBSITE_DIR / "PPICON.png")
    if (ASSETS_DIR / "PPTITLE.png").exists(): shutil.copy(ASSETS_DIR / "PPTITLE.png", WEBSITE_DIR / "PPTITLE.png")
    if (ASSETS_DIR / "logo.png").exists(): shutil.copy(ASSETS_DIR / "logo.png", WEBSITE_DIR / "logo.png")

    items = [p for p in ARTICLES_DIR.rglob("*.html") if p.name != "index.html"]
    items = sorted(items, key=os.path.getmtime, reverse=True)

    categories = [d.name for d in ARTICLES_DIR.iterdir() if d.is_dir()]
    nav_links = "".join([f'<a href="articles/{cat}/index.html">{cat.replace("-", " ").title()}</a>' for cat in categories])
    nav_html = f'<nav class="site-nav"><a href="index.html">Home</a>{nav_links}</nav>'

    # --- HANDLE EMPTY STATE ---
    if not items:
        (WEBSITE_DIR / "index.html").write_text(f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8"><title>Pahayagang Palanyag</title>
    <link rel="icon" href="favicon.png"><link rel="stylesheet" href="style.css">
    <link href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@700;900&family=Inter:wght@400;600&display=swap" rel="stylesheet">
</head>
<body>
    <div class="top-bar"><div class="container"><span>{datetime.datetime.now().strftime('%A, %B %d, %Y')}</span> <span>Paranaque, Metro Manila</span></div></div>
    <header class="site-header"><div class="container"><a href="index.html" class="header-brand"><img src="PPICON.png" class="header-icon"><img src="PPTITLE.png" class="header-text-img"></a></div></header>
    {nav_html}
    <main class="container" style="text-align:center; padding: 150px 20px;">
        <h2 style="font-family: var(--serif); font-size: 2rem; color: #666;">No articles published yet.</h2>
        <p style="margin-top:20px; color:#888;">The engine is running. New stories will appear here.</p>
    </main>
</body>
</html>""")
        print("[WEB] Homepage cleared. No articles.")
        return
    # ------------------------

    def get_true_title(p):
        try:
            soup = BeautifulSoup(p.read_text(encoding='utf-8'), 'html.parser')
            h1 = soup.find('h1', class_='article-title')
            if h1: return h1.text.strip()
        except: pass
        return p.stem.replace("-", " ").title()

    featured = items[0]
    f_title = get_true_title(featured)
    f_img = featured.with_suffix('.jpg').name
    f_cat = featured.parent.name
    
    hero_html = f"""
    <div class="top-layout">
        <div class="hero-main">
            <a href="articles/{f_cat}/{featured.name}"><img src="articles/{f_cat}/{f_img}" class="hero-image"></a>
            <h1><a href="articles/{f_cat}/{featured.name}">{f_title}</a></h1>
            <a href="articles/{f_cat}/{featured.name}" class="read-more">Read Full Story &rarr;</a>
        </div>
        <div class="hero-sidebar">
    """
    for p in items[1:4]:
        t = get_true_title(p)
        i = p.with_suffix('.jpg').name
        c = p.parent.name
        hero_html += f'<div class="sidebar-story"><img src="articles/{c}/{i}"><h3><a href="articles/{c}/{p.name}">{t}</a></h3></div>'
    hero_html += "</div></div>"

    grid_cards = ""
    for p in items[4:10]:
        t = get_true_title(p)
        i = p.with_suffix('.jpg').name
        c = p.parent.name
        grid_cards += f'<a href="articles/{c}/{p.name}" class="news-card"><img src="articles/{c}/{i}"><h2>{t}</h2></a>'

    (WEBSITE_DIR / "index.html").write_text(f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8"><title>Pahayagang Palanyag</title>
    <link rel="icon" href="favicon.png"><link rel="stylesheet" href="style.css">
    <link href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@700;900&family=Inter:wght@400;600&display=swap" rel="stylesheet">
</head>
<body>
    <div class="top-bar"><div class="container"><span>{datetime.datetime.now().strftime('%A, %B %d, %Y')}</span> <span>Paranaque, Metro Manila</span></div></div>
    <header class="site-header"><div class="container"><a href="index.html" class="header-brand"><img src="PPICON.png" class="header-icon"><img src="PPTITLE.png" class="header-text-img"></a></div></header>
    {nav_html}
    <main class="container">
        {hero_html}
        <div class="section-header"><h2 class="section-title">Featured Stories</h2></div>
        <div class="news-grid">{grid_cards}</div>
    </main>
</body>
</html>""")

    for cat in categories:
        cat_dir = ARTICLES_DIR / cat
        cat_items = [p for p in cat_dir.glob("*.html") if p.name != "index.html"]
        cat_items = sorted(cat_items, key=os.path.getmtime, reverse=True)
        if not cat_items: continue
        
        cat_cards = ""
        for p in cat_items:
            t = get_true_title(p)
            i = p.with_suffix('.jpg').name
            cat_cards += f'<a href="{p.name}" class="news-card"><img src="{i}"><h2>{t}</h2></a>'
        
        (cat_dir / "index.html").write_text(f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8"><title>{cat.title()} | Pahayagang Palanyag</title>
    <link rel="icon" href="../../favicon.png"><link rel="stylesheet" href="../../style.css">
    <link href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@700;900&family=Inter:wght@400;600&display=swap" rel="stylesheet">
</head>
<body>
    <header class="site-header"><div class="container"><a href="../../index.html" class="header-brand"><img src="../../PPICON.png" class="header-icon"><img src="../../PPTITLE.png" class="header-text-img"></a></div></header>
    {nav_html.replace('href="articles/', 'href="../')} 
    <main class="container"><h2 class="section-title">{cat.replace("-", " ").title()}</h2><div class="news-grid">{cat_cards}</div></main>
</body>
</html>""")

# ---------- MAIN AI CYCLE (Fixes Issue 2) ----------
def full_cycle():
    generate_favicon()
    articles = fetch_articles(2)
    if not articles: return
    art = articles[0]
    
    try: data = ai_write(art)
    except Exception as e:
        print(f"[AI] failed: {e}"); return

    source_img = ASSETS_DIR / "source_image.jpg"
    download_image(art["link"], source_img, data["headline"])
    
    social_img = DRAFTS_DIR / "draft_social.jpg"
    web_img = DRAFTS_DIR / "draft_web.jpg"
    compose_image(source_img, data["headline"], data["image_credit"], social_img, web_img)

    # Load into Dashboard
    CURRENT_DRAFT["data"] = data
    CURRENT_DRAFT["source_img"] = source_img
    CURRENT_DRAFT["social_img"] = social_img
    CURRENT_DRAFT["web_img"] = web_img
    CURRENT_DRAFT["status"] = "waiting"
    
    print("\n" + "="*50)
    print(f"👉 AI DRAFT READY: http://localhost:5000")
    print("="*50 + "\n")
    
    # Wait for approval (Dashboard thread handles the actual publishing)
    while CURRENT_DRAFT["status"] == "waiting":
        time.sleep(2)
    
    # Let the publisher thread finish before starting the next 4-hour cycle
    time.sleep(5)

# ---------- UTILS ----------
def generate_favicon():
    ppicon_path = ASSETS_DIR / "PPICON.png"
    favicon_path = WEBSITE_DIR / "favicon.png"
    if ppicon_path.exists():
        try:
            size = 128
            favicon = Image.new("RGBA", (size, size), (255, 255, 255, 0))
            draw = ImageDraw.Draw(favicon)
            draw.ellipse((0, 0, size, size), fill=(255, 255, 255, 255))
            icon = Image.open(ppicon_path).convert("RGBA")
            icon.thumbnail((size-20, size-20))
            favicon.paste(icon, ((size - icon.width) // 2, (size - icon.height) // 2), icon)
            favicon.save(favicon_path, "PNG")
        except Exception as e: print(f"[SYS] ❌ Favicon error: {e}")

def get_full_article(url):
    try:
        r = cffi_requests.get(url, impersonate="chrome120", timeout=15)
        soup = BeautifulSoup(r.text, 'html.parser')
        return "\n".join([p.get_text().strip() for p in soup.find_all('p') if len(p.get_text().strip()) > 40])[:4000]
    except: return ""

def download_image(url, path, headline):
    print(f"[IMG] Attempting to scrape: {url}")
    try:
        r = cffi_requests.get(url, impersonate="chrome120", timeout=15)
        soup = BeautifulSoup(r.text, 'html.parser')
        img_url = None
        
        # 1. Meta tags (skip if it's a generic logo)
        for tag in [{"property": "og:image"}, {"name": "twitter:image"}, {"itemprop": "image"}]:
            found = soup.find("meta", attrs=tag)
            if found and found.get("content"):
                content = found["content"]
                if not any(x in content.lower() for x in ['logo', 'icon', 'default', 'avatar']):
                    img_url = content
                    break
                    
        # 2. srcset
        if not img_url:
            for img in soup.find_all('img'):
                if img.get('srcset'):
                    highest_res = img['srcset'].split(',')[-1].strip().split(' ')[0]
                    if highest_res.startswith('http') and not any(x in highest_res.lower() for x in ['logo', 'icon']): 
                        img_url = highest_res
                        break
                        
        # 3. src / data-src
        if not img_url:
            for img in soup.find_all('img'):
                src = img.get('data-src') or img.get('src')
                if src and ('upload' in src or 'image' in src) and not any(x in src.lower() for x in ['logo', 'icon', 'avatar']):
                    img_url = src
                    break
                    
        if img_url:
            if img_url.startswith('//'): 
                img_url = 'https:' + img_url
            else: 
                img_url = urljoin(url, img_url)
                
            print(f"[IMG] Found image URL: {img_url[:80]}")
            
            # FIX: Only pass Referer. Let curl_cffi handle the User-Agent.
            headers = {'Referer': url}
            img_resp = cffi_requests.get(img_url, headers=headers, impersonate="chrome120", timeout=15)
            
            if 'image' in img_resp.headers.get('Content-Type', '').lower():
                with open(path, 'wb') as f: 
                    f.write(img_resp.content)
                print(f"[IMG] ✅ Successfully downloaded image.")
                return True
            else:
                print(f"[IMG] ❌ URL returned non-image: {img_resp.headers.get('Content-Type')}")
        else:
            print("[IMG] ❌ No image URL found on page.")
            
    except Exception as e:
        print(f"[IMG] ❌ Scrape failed: {e}")
        
    print("[IMG] 🎨 Generating fallback gradient...")
    Image.new("RGB", (1200, 630), (10, 25, 50)).save(path)
    return True

def load_context(): return CONTEXT_FILE.read_text() if CONTEXT_FILE.exists() else ""

DEFAULT_SOURCES = {"tier_1_gov": ["https://www.paranaquecity.gov.ph/feed/", "https://pna.gov.ph/feed"], "tier_2_news": ["https://www.inquirer.net/fullfeed", "https://www.philstar.com/rss/nation", "https://www.rappler.com/feed/", "https://news.abs-cbn.com/feed/rss", "https://www.gmanetwork.com/news/rss/nation/", "https://mb.com.ph/feed/", "https://www.manilatimes.net/feed/", "https://businessmirror.com.ph/feed/"]}

def load_sources():
    if SOURCES_FILE.exists(): return json.loads(SOURCES_FILE.read_text())
    SOURCES_FILE.write_text(json.dumps(DEFAULT_SOURCES, indent=2))
    return DEFAULT_SOURCES

def fetch_articles(limit_per_tier=2):
    articles = []
    for tier, urls in load_sources().items():
        if tier == "tier_3_social": continue
        for url in urls:
            try:
                feed = feedparser.parse(url)
                for entry in feed.entries[:limit_per_tier]:
                    articles.append({"title": entry.title, "link": entry.link, "summary": entry.get("summary", ""), "source": feed.feed.get("title", url), "tier": tier})
            except: pass
    articles.sort(key=lambda x: x["tier"])
    return articles

if __name__ == "__main__":
    print("Starting Pahayagang Palanyag...")
    threading.Thread(target=run_server, daemon=True).start()
    
    if "--editor" in sys.argv:
        print("\n🖋️  RUNNING IN EDITOR-ONLY MODE. Go to: http://localhost:5000/write")
        while True: time.sleep(1)
    else:
        print("\n🤖 RUNNING FULL AI ENGINE.")
        full_cycle()
        schedule.every(4).hours.do(full_cycle)
        while True:
            schedule.run_pending()
            time.sleep(60)