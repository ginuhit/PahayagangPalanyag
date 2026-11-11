# engine.py
import os, json, re, time, datetime, hashlib, shutil, random, threading, subprocess, sys, urllib.request, uuid
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
QUEUE_FILE   = DRAFTS_DIR / "queue.json"
ROTATION_FILE = BASE_DIR / "rotation.txt"

for d in (WEBSITE_DIR, ARTICLES_DIR, DRAFTS_DIR, ASSETS_DIR):
    d.mkdir(exist_ok=True)

MODEL_NAME   = "gemma4:12b"
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

# ---------- GLOBAL STATE & LOCKS ----------
publish_lock = threading.Lock()
queue_lock = threading.RLock() 
PENDING_DRAFTS = {}

# ---------- SOURCES & ROTATION ----------
DEFAULT_SOURCES = {
    "Politics": ["https://www.inquirer.net/fullfeed", "https://pna.gov.ph/feed", "https://www.philstar.com/rss/nation"],
    "Metro Manila": ["https://www.rappler.com/feed/", "https://mb.com.ph/feed/"],
    "Sports": ["https://sports.inquirer.net/feed"],
    "Technology": ["https://technology.inquirer.net/feed"],
    "Entertainment": ["https://entertainment.inquirer.net/feed"]
}

def load_sources():
    if SOURCES_FILE.exists(): 
        return json.loads(SOURCES_FILE.read_text())
    SOURCES_FILE.write_text(json.dumps(DEFAULT_SOURCES, indent=2))
    return DEFAULT_SOURCES

def get_next_category():
    sources = load_sources()
    cats = list(sources.keys())
    
    # FIX: Handle empty sources.json gracefully
    if not cats: 
        return None
        
    last_index = int(ROTATION_FILE.read_text().strip()) if ROTATION_FILE.exists() else -1
    next_index = (last_index + 1) % len(cats)
    ROTATION_FILE.write_text(str(next_index))
    return cats[next_index]

def fetch_article_for_category(category):
    sources = load_sources()
    urls = sources.get(category, [])
    for url in urls:
        try:
            feed = feedparser.parse(url)
            if feed.entries:
                entry = feed.entries[0]
                return {
                    "title": entry.title, "link": entry.link, 
                    "summary": entry.get("summary", ""), "source": feed.feed.get("title", url), "category": category
                }
        except Exception as e:
            print(f"[RSS] Failed to fetch {url}: {e}")
    return None

# ---------- PORTABLE FONTS & CLEANUP ----------
def get_font(font_type="bold", size=40):
    font_map = {
        "bold": ("Inter-Bold.ttf", "https://github.com/google/fonts/raw/main/ofl/inter/Inter%5Bopsz,wght%5D.ttf"),
        "serif": ("Playfair-Black.ttf", "https://github.com/google/fonts/raw/main/ofl/playfairdisplay/PlayfairDisplay%5Bwght%5D.ttf"),
        "small": ("Inter-Regular.ttf", "https://github.com/google/fonts/raw/main/ofl/inter/Inter%5Bopsz,wght%5D.ttf")
    }
    filename, url = font_map.get(font_type, font_map["bold"])
    font_path = ASSETS_DIR / filename
    if font_type == "bold" and os.path.exists("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"):
        try: return ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", size)
        except: pass
    if not font_path.exists():
        try: urllib.request.urlretrieve(url, font_path)
        except: return ImageFont.load_default()
    try: return ImageFont.truetype(str(font_path), size)
    except: return ImageFont.load_default()

def cleanup_old_files():
    now = time.time()
    cutoff = now - (3 * 86400)
    for folder in [DRAFTS_DIR, ASSETS_DIR]:
        for f in folder.glob("*.jpg"):
            if f.stat().st_mtime < cutoff:
                try: f.unlink()
                except: pass

# ---------- QUEUE PERSISTENCE ----------
def save_queue():
    with queue_lock:
        serializable = {}
        for k, v in PENDING_DRAFTS.items():
            serializable[k] = {
                "data": v["data"], "source_img": str(v["source_img"]),
                "social_img": str(v["social_img"]), "web_img": str(v["web_img"]), "status": v["status"]
            }
        QUEUE_FILE.write_text(json.dumps(serializable, indent=2))

def load_queue():
    global PENDING_DRAFTS
    if QUEUE_FILE.exists():
        try:
            data = json.loads(QUEUE_FILE.read_text())
            with queue_lock:
                for k, v in data.items():
                    PENDING_DRAFTS[k] = {
                        "data": v["data"], "source_img": Path(v["source_img"]),
                        "social_img": Path(v["social_img"]), "web_img": Path(v["web_img"]), "status": v["status"]
                    }
        except Exception as e: print(f"[SYS] Could not load queue: {e}")

# ---------- PUBLISHING ----------
def publish_article(draft_id):
    with publish_lock:
        draft = PENDING_DRAFTS.get(draft_id)
        if not draft or draft["status"] in ["published", "error"]: return
        
        draft["status"] = "published"
        save_queue()
        
        print(f"\n[PUBLISH] Lock acquired for: {draft['data']['headline'][:30]}...")
        try:
            cat_slug = re.sub(r"[^a-z0-9]+", "-", draft["data"].get("category", "News").lower()).strip("-")
            slug = re.sub(r"[^a-z0-9]+", "-", draft["data"]["headline"].lower()).strip("-")[:120]
            article_url = f"https://pahayagangpalanyag.pages.dev/articles/{cat_slug}/{slug}.html"
            
            print("[PUBLISH] 1/4: Sending to Facebook...")
            publish_to_facebook(draft["social_img"], f"{draft['data'].get('social_caption', '')}\n\nRead here: {article_url}")
            
            print("[PUBLISH] 2/4: Writing HTML...")
            publish_to_website(draft["data"], Path(draft["web_img"]).name)
            update_homepage()
            
            print("[PUBLISH] 3/4: Staging Git changes...")
            subprocess.run(["git", "add", "."], check=True, cwd=WEBSITE_DIR, timeout=60, stdin=subprocess.DEVNULL)
            subprocess.run(["git", "commit", "-m", f"Publish: {draft['data']['headline'][:30]}"], check=True, cwd=WEBSITE_DIR, timeout=60, stdin=subprocess.DEVNULL)
            
            print("[PUBLISH] 4/4: Pushing to GitHub...")
            subprocess.run(["git", "push"], check=True, cwd=WEBSITE_DIR, timeout=60, stdin=subprocess.DEVNULL)
            
            print(f"[PUBLISH] ✅ Done. Live on Facebook and Website.")
            
            with queue_lock:
                if draft_id in PENDING_DRAFTS: del PENDING_DRAFTS[draft_id]
            save_queue()
            
        except subprocess.TimeoutExpired:
            print("[GIT ERROR] Git operation timed out. Check GitHub connection.")
            draft["status"] = "error"
        except subprocess.CalledProcessError:
            print("[GIT ERROR] Git command failed. Ensure Git credentials are saved.")
            draft["status"] = "error"
        except Exception as e:
            print(f"[PUBLISH ERROR] {e}")
            draft["status"] = "error"
        save_queue()

# ---------- WEB SERVER (Flask) ----------
app = Flask(__name__)

@app.route('/', methods=['GET'])
def dashboard():
    if request.args.get('clear') == 'true':
        with queue_lock: PENDING_DRAFTS.clear()
        save_queue()
        return "<script>window.location.href='/';</script>"
    
    if request.args.get('delete'):
        with queue_lock:
            if request.args.get('delete') in PENDING_DRAFTS: del PENDING_DRAFTS[request.args.get('delete')]
        save_queue()
        return "<script>window.location.href='/';</script>"

    if not PENDING_DRAFTS:
        return "<div style='font-family:sans-serif; background:#111; color:#eee; height:100vh; padding:50px; text-align:center;'><h1>No pending drafts.</h1><p>The AI engine is running or sleeping.</p><a href='/write' style='color:#1e5fd0; font-size:20px; text-decoration:none;'>✍️ Write a Custom Article</a></div>"
    
    html = """
    <html><head><style>
        body { font-family: sans-serif; background: #111; color: #eee; max-width: 800px; margin: auto; padding: 40px; }
        h1 { font-family: serif; border-bottom: 2px solid #333; padding-bottom: 10px; display: flex; justify-content: space-between; align-items: center; }
        .draft-card { background: #222; padding: 20px; margin-bottom: 20px; border-radius: 8px; display: flex; align-items: center; justify-content: space-between; }
        .draft-card a { color: #1e5fd0; text-decoration: none; font-size: 1.2rem; font-weight: bold; }
        .draft-card a:hover { text-decoration: underline; }
        .tag { background: #444; padding: 4px 8px; border-radius: 4px; font-size: 0.8rem; text-transform: uppercase; margin-left: 10px; }
        .btn-write { display: inline-block; padding: 15px 30px; font-size: 18px; margin-bottom: 30px; background: #1e5fd0; color: white; text-decoration: none; border-radius: 5px; font-weight: bold; }
        .btn-clear { background: #c62828; color: white; padding: 8px 15px; text-decoration: none; border-radius: 5px; font-size: 0.9rem; }
        .btn-delete { background: #c62828; color: white; padding: 5px 10px; text-decoration: none; border-radius: 4px; font-size: 0.8rem; margin-left: 10px; }
    </style></head><body>
        <h1>Pending Drafts ({{ count }}) <a href="/?clear=true" class="btn-clear" onclick="return confirm('Clear all pending drafts?')">🗑️ Clear Queue</a></h1>
        <a href="/write" class="btn-write">✍️ Write a Custom Article</a>
        {% for id, draft in drafts.items() %}
            <div class="draft-card" style="opacity: {{ '0.5' if draft.status != 'waiting' else '1' }};">
                <div>
                    <a href="/draft/{{ id }}">{{ draft.data.headline }}</a>
                    <span class="tag">{{ draft.data.article_type }}</span>
                    <span class="tag">{{ draft.data.category }}</span>
                    {% if draft.status == 'published' %}
                        <span class="tag" style="background:#2e7d32;">Publishing...</span>
                    {% elif draft.status == 'error' %}
                        <span class="tag" style="background:#c62828;">ERROR</span>
                        <a href="/?delete={{ id }}" class="btn-delete" onclick="return confirm('Delete failed draft?')">🗑️ Force Delete</a>
                    {% endif %}
                </div>
                <a href="/draft/{{ id }}">Review &rarr;</a>
            </div>
        {% endfor %}
    </body></html>
    """
    return render_template_string(html, drafts=PENDING_DRAFTS, count=len(PENDING_DRAFTS))

def recompose_draft(draft_id):
    draft = PENDING_DRAFTS.get(draft_id)
    if draft:
        compose_image(draft["source_img"], draft["data"]["headline"], draft["data"]["image_credit"], draft["social_img"], draft["web_img"])

@app.route('/draft/<draft_id>', methods=['GET', 'POST'])
def view_draft(draft_id):
    draft = PENDING_DRAFTS.get(draft_id)
    if not draft: return "Draft not found or already published.", 404
    
    if request.method == 'POST':
        action = request.form.get('action')
        with queue_lock:
            draft["data"]["headline"] = request.form.get('headline')
            draft["data"]["byline"] = request.form.get('byline')
            draft["data"]["editorial"] = request.form.get('editorial')
            save_queue()
        
        if action == 'update':
            threading.Thread(target=recompose_draft, args=(draft_id,)).start()
            return "<script>alert('Updating in background!'); window.location.reload();</script>"
        elif action == 'approve':
            threading.Thread(target=publish_article, args=(draft_id,)).start()
            return "<script>alert('✅ Approved! Publishing in background...'); window.location.href='/';</script>"
        elif action == 'reject':
            with queue_lock:
                if draft_id in PENDING_DRAFTS: del PENDING_DRAFTS[draft_id]
            save_queue()
            return "<script>alert('❌ Draft Rejected.'); window.location.href='/';</script>"

    html = """
    <html><head><style>
        body { font-family: sans-serif; background: #111; color: #eee; max-width: 1000px; margin: auto; padding: 20px; }
        img { max-width: 100%; border-radius: 8px; margin-bottom: 20px; }
        h1 { font-family: serif; border-bottom: 2px solid #333; padding-bottom: 10px; }
        .btn { padding: 15px 30px; font-size: 18px; margin: 10px; border: none; border-radius: 5px; cursor: pointer; color: white;}
        .approve { background: #2e7d32; } .reject { background: #c62828; } .update { background: #1e5fd0; }
        input[type="text"] { width: 100%; padding: 12px; margin-bottom: 15px; background: #222; color: white; border: 1px solid #444; border-radius: 5px; font-size: 16px; }
        textarea { width: 100%; height: 300px; padding: 12px; background: #222; color: white; border: 1px solid #444; border-radius: 5px; font-family: inherit; font-size: 16px; }
        label { font-weight: bold; color: #aaa; margin-bottom: 5px; display: block; margin-top: 15px; }
        .flex { display: flex; gap: 20px; } .flex > div { flex: 1; }
        .actions { text-align: center; margin-top: 30px; }
        .back { color: #1e5fd0; text-decoration: none; display: inline-block; margin-bottom: 20px; }
    </style></head><body>
        <a href="/" class="back">&larr; Back to Queue</a>
        <h1>Review: {{ type }}</h1>
        <div class="flex">
            <div><img src="/social_image/{{ id }}"><p style="text-align:center; color:#888;">Facebook Version</p></div>
            <div><img src="/web_image/{{ id }}"><p style="text-align:center; color:#888;">Website Version</p></div>
        </div>
        <form method="POST">
            <label>Headline</label><input type="text" name="headline" value="{{ headline }}">
            <label>Byline</label><input type="text" name="byline" value="{{ byline }}">
            <label>Editorial Text</label><textarea name="editorial">{{ editorial }}</textarea>
            <div class="actions">
                <button type="submit" name="action" value="update" class="btn update">💾 Update Draft</button>
                <button type="submit" name="action" value="approve" class="btn approve">✅ Approve & Publish</button>
                <button type="submit" name="action" value="reject" class="btn reject">❌ Reject</button>
            </div>
        </form>
    </body></html>
    """
    return render_template_string(html, 
        id=draft_id, type=draft["data"]["article_type"],
        headline=draft["data"]["headline"], byline=draft["data"]["byline"], editorial=draft["data"]["editorial"])

@app.route('/social_image/<draft_id>')
def serve_social(draft_id):
    try: return open(PENDING_DRAFTS[draft_id]["social_img"], "rb").read()
    except: return "Image not found", 404

@app.route('/web_image/<draft_id>')
def serve_web(draft_id):
    try: return open(PENDING_DRAFTS[draft_id]["web_img"], "rb").read()
    except: return "Image not found", 404

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
        if file and file.filename != '': file.save(source_img_path)
        else: Image.new("RGB", (1200, 630), (20, 40, 80)).save(source_img_path)

        draft_id = str(uuid.uuid4())[:8]
        social_img = DRAFTS_DIR / f"draft_social_{draft_id}.jpg"
        web_img = DRAFTS_DIR / f"draft_web_{draft_id}.jpg"
        compose_image(source_img_path, headline, image_credit, social_img, web_img)

        with queue_lock:
            PENDING_DRAFTS[draft_id] = {
                "data": {"article_type": "Custom", "headline": headline, "byline": byline, "category": category,
                         "editorial": editorial, "social_caption": f"{headline}\n\nRead more on Pahayagang Palanyag.", "image_credit": image_credit},
                "source_img": source_img_path, "social_img": social_img, "web_img": web_img, "status": "waiting"
            }
            save_queue()
        return "<script>alert('Added to Queue!'); window.location.href='/';</script>"

    return render_template_string("""
    <html><head><style>
        body { font-family: sans-serif; background: #111; color: #eee; max-width: 800px; margin: auto; padding: 40px; }
        h1 { font-family: serif; border-bottom: 2px solid #333; padding-bottom: 10px; }
        .btn { padding: 15px 30px; font-size: 18px; margin-top: 20px; border: none; border-radius: 5px; cursor: pointer; color: white; background: #1e5fd0; width: 100%; font-weight:bold;}
        input, textarea, select { width: 100%; padding: 12px; margin-bottom: 15px; background: #222; color: white; border: 1px solid #444; border-radius: 5px; font-size: 16px; }
        textarea { height: 300px; } label { font-weight: bold; color: #aaa; margin-bottom: 5px; display: block; margin-top: 15px; }
    </style></head><body>
        <h1>Write Custom Article</h1><a href="/" style="color:#1e5fd0; text-decoration:none;">&larr; Back to Queue</a>
        <form method="POST" enctype="multipart/form-data">
            <label>Headline</label><input type="text" name="headline" required>
            <label>Byline</label><input type="text" name="byline" value="By the Editorial Board">
            <label>Category</label><select name="category"><option>News</option><option>Politics</option><option>Opinion</option><option>Sports</option><option>Entertainment</option><option>Technology</option></select>
            <label>Image Credit</label><input type="text" name="image_credit" value="Pahayagang Palanyag">
            <label>Upload Cover Image</label><input type="file" name="image" accept="image/*" required>
            <label>Editorial Text</label><textarea name="editorial" required></textarea>
            <button type="submit" class="btn">Add to Queue</button>
        </form>
    </body></html>""")

def run_server():
    app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)

# ---------- AI WRITER (DUAL-PERSONA) ----------
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

EDITOR_PROMPT = """You are the Editor-in-Chief of Pahayagang Palanyag.
Your writing is analytical, factual, and thought-provoking. You write deep socio-political analyses.

### LOCAL CONTEXT
{context}

### SOURCE ARTICLE
Source: {source}
Title: {title}
Text:

### TASK
Write a 4-6 paragraph EDITORIAL (minimum 400 words). Do not use clichés. 
Analyze the broader national or human impact. Do not force a local Parañaque angle if it doesn't exist.
Use the byline "Palanyag Editor".
Write a 2-sentence social hook.
CRITICAL: Provide an image credit. Output the name of the news organization (e.g., "INQUIRER.net"). Do NOT output "None".
"""

REPORTER_PROMPT = """You are a concise News Reporter for Pahayagang Palanyag.
Your writing is straight, factual, and fast.

### CATEGORY: {category}

### SOURCE ARTICLE
Source: {source}
Title: {title}
Text:

### TASK
Write a 2-3 paragraph NEWS BRIEF (maximum 200 words). 
Summarize the key facts clearly. Do not include personal opinions or deep analysis. Just tell the reader what happened specifically in the context of {category}.
Use the byline "Palanyag Reporter".
Write a 1-sentence social hook.
CRITICAL: Provide an image credit. Output the name of the news organization (e.g., "INQUIRER.net"). Do NOT output "None".
"""

def ai_write(article, mode="News"):
    full_text = get_full_article(article["link"]) or article.get("summary", "")
    context_str = load_context()
    category = article.get("category", "News") # Preserve actual category
    
    if mode == "Editorial":
        prompt = EDITOR_PROMPT.format(context=context_str, source=article["source"], title=article["title"], text=full_text[:3500])
    else:
        # FIX: Inject category into reporter prompt so it knows the context
        prompt = REPORTER_PROMPT.format(category=category, source=article["source"], title=article["title"], text=full_text[:2000])
    
    resp = ollama.chat(
        model=MODEL_NAME, messages=[{"role": "user", "content": prompt}],
        format=SCHEMA, options={"temperature": 0.5, "num_predict": 4096, "num_ctx": 8192}
    )
    
    data = json.loads(resp["message"]["content"])
    data["article_type"] = mode
    data["category"] = category # Ensure AI's output matches the feed's actual category
    return data

# ---------- IMAGE COMPOSER ----------
def compose_image(source_img_path, headline, credit, social_out_path, web_out_path):
    original_img = Image.open(source_img_path).convert("RGBA")
    logo_path = ASSETS_DIR / "logo.png"
    
    font_bold = get_font("bold", 40)
    font_small = get_font("small", 16)

    clean_credit = str(credit).strip() if credit else ""
    if clean_credit.lower() in ["none", "null", "n/a", ""]: clean_credit = "Photo via Source Article"
    credit_text = f"📷 {clean_credit}"

    def add_overlays(base_img, is_social=False):
        draw = ImageDraw.Draw(base_img, 'RGBA')
        
        if logo_path.exists():
            logo = Image.open(logo_path).convert("RGBA")
            box_size = 250 if is_social else 180
            logo.thumbnail((box_size - 30, box_size - 30))
            box_x1 = base_img.width - box_size - 20
            draw.rectangle([(box_x1, 0), (base_img.width - 20, box_size)], fill=(255, 255, 255, 255))
            base_img.paste(logo, (box_x1 + (box_size - logo.width)//2, (box_size - logo.height)//2), logo)

        draw.rectangle([(0, base_img.height - 40), (base_img.width, base_img.height)], fill=(0, 0, 0, 200))
        draw.text((20, base_img.height - 30), credit_text, font=font_small, fill=(220, 220, 220))

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
    try:
        with open(image_path, "rb") as f:
            r = requests.post(url, data={"message": caption, "access_token": FACEBOOK_PAGE_ACCESS_TOKEN}, files={"source": f}, timeout=30)
        if r.status_code == 200: print(f"[FB] ✅ Successfully posted!")
        else: print(f"[FB] ❌ Error: {r.text}")
    except Exception as e: print(f"[FB] ❌ Connection failed: {e}")

def publish_to_website(article_data, image_filename):
    cat_slug = re.sub(r"[^a-z0-9]+", "-", article_data.get("category", "News").lower()).strip("-")
    cat_dir = ARTICLES_DIR / cat_slug
    cat_dir.mkdir(exist_ok=True)
    
    art_slug = re.sub(r"[^a-z0-9]+", "-", article_data["headline"].lower()).strip("-")[:120]
    new_image_name = f"{art_slug}.jpg"
    paragraphs = ''.join(f'<p class="mb-6 text-lg text-zinc-700 leading-relaxed">{p}</p>' for p in article_data['editorial'].split(chr(10)) if p.strip())
    
     byline_text = article_data.get('byline', '').strip()
    if len(byline_text) < 3: 
        byline_text = "Palanyag Editor" if article_data.get("article_type") == "Editorial" else "Palanyag Reporter"
    
    clean_credit = str(article_data.get('image_credit', '')).strip()
    if clean_credit.lower() in ["none", "null", "n/a", ""]: clean_credit = "Photo via Source Article"

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{article_data['headline']} | Pahayagang Palanyag</title>
    <link rel="icon" href="../../favicon.png">
    <script src="https://cdn.tailwindcss.com"></script>
    <link href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@700;900&family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
    <script>tailwind.config = {{ theme: {{ extend: {{ fontFamily: {{ serif: ['"Playfair Display"', 'serif'], sans: ['Inter', 'sans-serif'] }} }} }} }}</script>
    <style>body {{ background-color: #fff; color: #18181b; }}</style>
</head>
<body class="font-sans antialiased">
    <nav class="sticky top-0 z-50 border-b border-zinc-200 bg-white/80 backdrop-blur-md">
      <div class="mx-auto flex max-w-7xl items-center justify-between px-6 py-4">
        <a href="../../index.html" class="flex items-center gap-2"><img src="../../PPICON.png" class="h-10 w-auto"><img src="../../PPTITLE.png" class="h-8 w-auto mt-1"></a>
        <div class="hidden space-x-8 text-sm font-medium uppercase tracking-widest md:flex"><a href="../../index.html" class="hover:text-zinc-500 transition">Home</a></div>
      </div>
    </nav>
    <main class="mx-auto max-w-4xl px-6 py-16">
        <div class="text-center mb-12">
            <span class="text-xs font-bold uppercase tracking-[0.2em] text-zinc-500">{article_data.get('category', 'News')}</span>
            <h1 class="mt-4 font-serif text-4xl md:text-6xl font-black leading-tight">{article_data['headline']}</h1>
            <div class="mt-6 flex items-center justify-center text-sm text-zinc-500 font-medium"><span>{byline_text}</span><span class="mx-3">•</span><span>{datetime.datetime.now().strftime('%B %d, %Y')}</span></div>
        </div>
        <div class="mb-12"><img src="{new_image_name}" alt="{article_data['headline']}" class="w-full rounded-sm"><p class="mt-3 text-xs text-zinc-400 text-right">{clean_credit}</p></div>
        <article class="mx-auto max-w-2xl font-sans">{paragraphs}</article>
    </main>
    <footer class="mt-24 border-t border-zinc-200 bg-zinc-50 py-12"><div class="mx-auto max-w-7xl px-6 text-center"><div class="flex justify-center items-center gap-2 mb-4"><img src="../../PPICON.png" class="h-8 w-auto"><img src="../../PPTITLE.png" class="h-6 w-auto mt-1"></div><p class="text-xs text-zinc-500 uppercase tracking-widest">© {datetime.datetime.now().year} Pahayagang Palanyag. All rights reserved.</p></div></footer>
</body></html>"""
    (cat_dir / f"{art_slug}.html").write_text(html)
    shutil.copy(DRAFTS_DIR / image_filename, cat_dir / new_image_name)

def update_homepage():
    if (ASSETS_DIR / "PPICON.png").exists(): shutil.copy(ASSETS_DIR / "PPICON.png", WEBSITE_DIR / "PPICON.png")
    if (ASSETS_DIR / "PPTITLE.png").exists(): shutil.copy(ASSETS_DIR / "PPTITLE.png", WEBSITE_DIR / "PPTITLE.png")
    if (ASSETS_DIR / "logo.png").exists(): shutil.copy(ASSETS_DIR / "logo.png", WEBSITE_DIR / "logo.png")

    items = [p for p in ARTICLES_DIR.rglob("*.html") if p.name != "index.html"]
    items = sorted(items, key=os.path.getmtime, reverse=True)

    categories = [d.name for d in ARTICLES_DIR.iterdir() if d.is_dir()]
    nav_links = "".join([f'<a href="articles/{cat}/index.html" class="hover:text-zinc-500 transition">{cat.replace("-", " ").title()}</a>' for cat in categories])

    if not items:
        (WEBSITE_DIR / "index.html").write_text(f"<html><body class='bg-white text-center pt-40 font-sans'><h1 class='text-4xl font-serif font-bold'>No articles yet.</h1></body></html>")
        return

    def get_article_info(p):
        try:
            soup = BeautifulSoup(p.read_text(encoding='utf-8'), 'html.parser')
            h1 = soup.find('h1')
            title = h1.text.strip() if h1 else p.stem.replace("-", " ").title()
            cat = p.parent.name.replace("-", " ")
            first_p = soup.find('p', class_='mb-6')
            excerpt = first_p.text.strip()[:150] + "..." if first_p else "Read the full story."
            return title, cat, excerpt
        except: return p.stem.replace("-", " ").title(), "News", "Read the full story."

    hero_title, hero_cat, hero_excerpt = get_article_info(items[0])
    hero_img = items[0].with_suffix('.jpg').name
    hero_path = f"articles/{items[0].parent.name}/{items[0].name}"

    hero_html = f"""
    <section class="group cursor-pointer mb-16">
      <a href="{hero_path}">
        <div class="overflow-hidden rounded-sm"><img src="articles/{items[0].parent.name}/{hero_img}" alt="Hero Image" class="h-[500px] w-full object-cover transition duration-700 group-hover:scale-105"></div>
        <div class="mt-8 max-w-3xl">
          <span class="text-xs font-bold uppercase tracking-[0.2em] text-zinc-500">{hero_cat}</span>
          <h1 class="mt-3 font-serif text-5xl font-black leading-tight md:text-6xl group-hover:text-zinc-600 transition">{hero_title}</h1>
          <p class="mt-4 text-lg text-zinc-600 leading-relaxed">{hero_excerpt}</p>
          <div class="mt-6 flex items-center text-sm text-zinc-400"><span>Read Full Story &rarr;</span></div>
        </div>
      </a>
    </section><hr class="border-zinc-200 mb-16">
    """

    grid_html = ""
    for p in items[1:5]:
        title, cat, excerpt = get_article_info(p)
        img = p.with_suffix('.jpg').name
        path = f"articles/{p.parent.name}/{p.name}"
        grid_html += f"""
        <article class="group cursor-pointer flex flex-col md:flex-row gap-6">
          <div class="w-full md:w-1/3 overflow-hidden rounded-sm"><a href="{path}"><img src="articles/{p.parent.name}/{img}" class="h-48 w-full object-cover transition group-hover:scale-105"></a></div>
          <div class="flex-1">
            <span class="text-[10px] font-bold uppercase tracking-widest text-zinc-400">{cat}</span>
            <h3 class="mt-2 font-serif text-2xl font-bold group-hover:text-zinc-600 transition"><a href="{path}">{title}</a></h3>
            <p class="mt-2 text-zinc-600 line-clamp-2">{excerpt}</p>
            <div class="mt-4 text-xs font-bold text-zinc-900 uppercase"><a href="{path}">Read Story &rarr;</a></div>
          </div>
        </article>"""

    sidebar_html = ""
    for i, p in enumerate(items[5:8]):
        title, cat, _ = get_article_info(p)
        path = f"articles/{p.parent.name}/{p.name}"
        sidebar_html += f"""
        <div class="flex gap-4 items-start">
          <span class="text-3xl font-serif text-zinc-200">0{i+1}</span>
          <div><a href="{path}" class="font-bold hover:text-zinc-500 transition leading-snug block">{title}</a><p class="text-xs text-zinc-400 mt-1 uppercase tracking-widest">{cat}</p></div>
        </div>"""

    (WEBSITE_DIR / "index.html").write_text(f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Pahayagang Palanyag</title>
  <link rel="icon" href="favicon.png"><script src="https://cdn.tailwindcss.com"></script>
  <link href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@700;900&family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
  <script>tailwind.config = {{ theme: {{ extend: {{ fontFamily: {{ serif: ['"Playfair Display"', 'serif'], sans: ['Inter', 'sans-serif'] }} }} }} }}</script>
  <style>body {{ background-color: #fff; color: #18181b; }}</style>
</head>
<body class="font-sans antialiased">
  <nav class="sticky top-0 z-50 border-b border-zinc-200 bg-white/80 backdrop-blur-md">
    <div class="mx-auto flex max-w-7xl items-center justify-between px-6 py-4">
      <a href="index.html" class="flex items-center gap-3"><img src="PPICON.png" class="h-12 w-auto"><img src="PPTITLE.png" class="h-9 w-auto mt-1"></a>
      <div class="hidden space-x-8 text-sm font-medium uppercase tracking-widest md:flex"><a href="index.html" class="hover:text-zinc-500 transition">Home</a>{nav_links}</div>
    </div>
  </nav>
  <main class="mx-auto max-w-7xl px-6 py-12">
    {hero_html}
    <div class="grid grid-cols-1 gap-16 lg:grid-cols-12">
      <div class="lg:col-span-8"><h2 class="mb-8 font-serif text-3xl font-bold border-b border-zinc-900 pb-2">Latest News</h2><div class="space-y-12">{grid_html}</div></div>
      <aside class="lg:col-span-4">
        <div class="sticky top-24">
          <h2 class="border-b border-zinc-900 pb-2 text-xs font-black uppercase tracking-[0.2em]">Trending</h2>
          <div class="mt-6 space-y-8">{sidebar_html}</div>
          <div class="mt-16 rounded-sm bg-zinc-100 p-8"><h3 class="font-serif text-xl font-bold">The Morning Briefing</h3><p class="mt-2 text-sm text-zinc-600">Get the most important stories delivered to your inbox every morning.</p><input type="email" placeholder="email@example.com" class="mt-4 w-full border border-zinc-300 bg-white px-4 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-zinc-400"><button class="mt-4 w-full bg-zinc-900 py-2 text-sm font-medium text-white transition hover:bg-zinc-700">Join Newsletter</button></div>
        </div>
      </aside>
    </div>
  </main>
  <footer class="mt-24 border-t border-zinc-200 bg-zinc-50 py-12"><div class="mx-auto max-w-7xl px-6 text-center"><div class="flex justify-center items-center gap-2 mb-4"><img src="PPICON.png" class="h-10 w-auto opacity-50 grayscale"><img src="PPTITLE.png" class="h-8 w-auto mt-1 opacity-50 grayscale"></div><p class="mt-4 text-xs text-zinc-500 uppercase tracking-widest">© {datetime.datetime.now().year} Pahayagang Palanyag. All rights reserved.</p></div></footer>
</body></html>""")

# ---------- MAIN AI CYCLE ----------
def full_cycle():
    cleanup_old_files()
    generate_favicon()
    
    # Try up to 5 categories until we find an article
    for attempt in range(5):
        category = get_next_category()
        if not category:
            print("[AI] No categories found in sources.json. Please add some.")
            return
            
        print(f"[AI] Fetching next story for category: {category}")
        art = fetch_article_for_category(category)
        
        if art:
            break # We found an article! Break the loop and proceed.
        else:
            print(f"[AI] No articles found for {category}. Trying next category...")
            
    if not art:
        print("[AI] Could not find articles in any category. Skipping cycle.")
        return
    
    # Determine Mode
    if category in ["Politics", "Metro Manila"]:
        mode = "Editorial"
    else:
        mode = "News"
        
    print(f"[AI] Processing as {mode}: {art['title'][:50]}")
    try: 
        data = ai_write(art, mode)
    except Exception as e:
        print(f"[AI] failed on {art['title']}: {e}"); return

    draft_id = str(uuid.uuid4())[:8]
    source_img = ASSETS_DIR / f"source_image_{draft_id}.jpg"
    download_image(art["link"], source_img, data["headline"])
    
    social_img = DRAFTS_DIR / f"draft_social_{draft_id}.jpg"
    web_img = DRAFTS_DIR / f"draft_web_{draft_id}.jpg"
    compose_image(source_img, data["headline"], data["image_credit"], social_img, web_img)

    with queue_lock:
        PENDING_DRAFTS[draft_id] = {
            "data": data, "source_img": source_img, "social_img": social_img, "web_img": web_img, "status": "waiting"
        }
    save_queue()

    print(f"\n👉 1 {mode} DRAFT READY ({category}). Go to: http://localhost:5000\n")
    try:
        requests.post(f"{TELEGRAM_API}/sendMessage", data={
            "chat_id": TELEGRAM_CHAT_ID, 
            "text": f"📰 *1 new {mode} draft ({category}) is waiting.*\n\nPlease check your dashboard: http://localhost:5000",
            "parse_mode": "Markdown"
        }, timeout=10)
    except Exception as e:
        print(f"[TG ERROR] Could not send Telegram message: {e}")

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
        
        # DEBUG: Print HTML length to see if Cloudflare blocked us
        print(f"[IMG] Downloaded HTML size: {len(r.text)} characters.")
        if len(r.text) < 2000:
            print("[IMG] ⚠️ HTML is too small. Cloudflare might be blocking the request.")

        img_urls = []
        
        # 1. Check Meta Tags
        for tag in [{"property": "og:image"}, {"name": "twitter:image"}, {"itemprop": "image"}]:
            found = soup.find("meta", attrs=tag)
            if found and found.get("content"):
                content = found["content"]
                if not any(x in content.lower() for x in ['logo', 'icon', 'default', 'avatar']):
                    img_urls.append(content)
                    
        # 2. Check srcset
        for img in soup.find_all('img'):
            srcset = img.get('srcset') or img.get('data-srcset')
            if srcset:
                highest_res = srcset.split(',')[-1].strip().split(' ')[0]
                if highest_res.startswith('http') and not any(x in highest_res.lower() for x in ['logo', 'icon']):
                    img_urls.append(highest_res)
                    
        # 3. Check standard image tags
        lazy_attrs = ['data-src', 'data-lazy-src', 'data-original', 'src']
        for img in soup.find_all('img'):
            for attr in lazy_attrs:
                src = img.get(attr)
                if src and not src.startswith('data:'):
                    img_urls.append(src)
                    break

        # 4. Regex Fallback (Scan the raw HTML for image extensions)
        if not img_urls:
            print("[IMG] No images found in HTML tags. Using Regex fallback...")
            # Find all URLs ending in common image extensions
            regex_pattern = r'https?://[^\s"\'<>]+?\.(?:jpg|jpeg|png|webp|avif)'
            matches = re.findall(regex_pattern, r.text)
            for match in matches:
                if not any(x in match.lower() for x in ['logo', 'icon', 'avatar']):
                    img_urls.append(match)

        # Deduplicate and remove bad URLs
        clean_img_urls = []
        for u in list(set(img_urls)):
            if u.startswith('//'): u = 'https:' + u
            elif not u.startswith('http'): u = urljoin(url, u)
            clean_img_urls.append(u)

        print(f"[IMG] Found {len(clean_img_urls)} potential image URLs. Attempting download...")

        # Try downloading each URL until one works
        headers = {'Referer': url, 'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
        for final_url in clean_img_urls:
            try:
                img_resp = cffi_requests.get(final_url, headers=headers, impersonate="chrome120", timeout=10)
                if 'image' in img_resp.headers.get('Content-Type', '').lower():
                    with open(path, 'wb') as f: 
                        f.write(img_resp.content)
                    print(f"[IMG] ✅ Successfully downloaded image: {final_url[:60]}")
                    return True
            except:
                continue # If this URL fails, try the next one

        print("[IMG] ❌ All image URLs failed to download.")
            
    except Exception as e: 
        print(f"[IMG] ❌ Scrape failed: {e}")
    
    print("[IMG] 🎨 Generating fallback gradient...")
    Image.new("RGB", (1200, 630), (10, 25, 50)).save(path)
    return True

def load_context(): return CONTEXT_FILE.read_text() if CONTEXT_FILE.exists() else ""

if __name__ == "__main__":
    print("Starting Pahayagang Palanyag...")
    load_queue()
    threading.Thread(target=run_server, daemon=True).start()
    
    if "--editor" in sys.argv:
        print("\n🖋️  RUNNING IN EDITOR-ONLY MODE. Go to: http://localhost:5000/write")
        while True: time.sleep(1)
    else:
        print("\n🤖 RUNNING FULL AI ENGINE (PULSE MODE).")
        full_cycle()
        schedule.every(4).hours.do(full_cycle)
        while True:
            schedule.run_pending()
            time.sleep(60)