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
    
    # Convert editorial into Tailwind-styled paragraphs
    paragraphs = ''.join(f'<p class="mb-6 text-lg text-zinc-700 leading-relaxed">{p}</p>' for p in article_data['editorial'].split(chr(10)) if p.strip())
    
    byline_text = article_data.get('byline', '').strip()
    if len(byline_text) < 3: byline_text = "By the Editorial Board"
    
    clean_credit = str(article_data.get('image_credit', '')).strip()
    if clean_credit.lower() in ["none", "null", "n/a", ""]: clean_credit = "Photo via Source Article"

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{article_data['headline']} | Pahayagang Palanyag</title>
    <link rel="icon" href="../../favicon.png">
    <script src="https://cdn.tailwindcss.com"></script>
    <link href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@700;900&family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
    <script>
      tailwind.config = {{
        theme: {{
          extend: {{
            fontFamily: {{ serif: ['"Playfair Display"', 'serif'], sans: ['Inter', 'sans-serif'] }}
          }}
        }}
      }}
    </script>
    <style>body {{ background-color: #fff; color: #18181b; }}</style>
</head>
<body class="font-sans antialiased">

    <!-- NAVIGATION -->
    <nav class="sticky top-0 z-50 border-b border-zinc-200 bg-white/80 backdrop-blur-md">
      <div class="mx-auto flex max-w-7xl items-center justify-between px-6 py-4">
        <a href="../../index.html" class="flex items-center gap-2">
            <img src="../../PPICON.png" class="h-10 w-auto">
            <img src="../../PPTITLE.png" class="h-8 w-auto mt-1">
        </a>
        <div class="hidden space-x-8 text-sm font-medium uppercase tracking-widest md:flex">
          <a href="../../index.html" class="hover:text-zinc-500 transition">Home</a>
        </div>
      </div>
    </nav>

    <main class="mx-auto max-w-4xl px-6 py-16">
        <!-- ARTICLE HEADER -->
        <div class="text-center mb-12">
            <span class="text-xs font-bold uppercase tracking-[0.2em] text-zinc-500">{article_data.get('category', 'News')}</span>
            <h1 class="mt-4 font-serif text-4xl md:text-6xl font-black leading-tight">{article_data['headline']}</h1>
            <div class="mt-6 flex items-center justify-center text-sm text-zinc-500 font-medium">
                <span>{byline_text}</span>
                <span class="mx-3">•</span>
                <span>{datetime.datetime.now().strftime('%B %d, %Y')}</span>
            </div>
        </div>

        <!-- HERO IMAGE -->
        <div class="mb-12">
            <img src="{new_image_name}" alt="{article_data['headline']}" class="w-full rounded-sm">
            <p class="mt-3 text-xs text-zinc-400 text-right">{clean_credit}</p>
        </div>

        <!-- ARTICLE CONTENT -->
        <article class="mx-auto max-w-2xl font-sans">
            {paragraphs}
        </article>
    </main>

    <footer class="mt-24 border-t border-zinc-200 bg-zinc-50 py-12">
      <div class="mx-auto max-w-7xl px-6 text-center">
        <div class="flex justify-center items-center gap-2 mb-4">
            <img src="../../PPICON.png" class="h-8 w-auto">
            <img src="../../PPTITLE.png" class="h-6 w-auto mt-1">
        </div>
        <p class="text-xs text-zinc-500 uppercase tracking-widest">© {datetime.datetime.now().year} Pahayagang Palanyag. All rights reserved.</p>
      </div>
    </footer>
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
            # Try to grab the first paragraph for the excerpt
            first_p = soup.find('p', class_='mb-6')
            excerpt = first_p.text.strip()[:150] + "..." if first_p else "Read the full story."
            return title, cat, excerpt
        except:
            return p.stem.replace("-", " ").title(), "News", "Read the full story."

    # HERO SECTION DATA
    hero_title, hero_cat, hero_excerpt = get_article_info(items[0])
    hero_img = items[0].with_suffix('.jpg').name
    hero_path = f"articles/{items[0].parent.name}/{items[0].name}"

    hero_html = f"""
    <section class="group cursor-pointer mb-16">
      <a href="{hero_path}">
        <div class="overflow-hidden rounded-sm">
          <img src="articles/{items[0].parent.name}/{hero_img}" alt="Hero Image" class="h-[500px] w-full object-cover transition duration-700 group-hover:scale-105">
        </div>
        <div class="mt-8 max-w-3xl">
          <span class="text-xs font-bold uppercase tracking-[0.2em] text-zinc-500">{hero_cat}</span>
          <h1 class="mt-3 font-serif text-5xl font-black leading-tight md:text-6xl group-hover:text-zinc-600 transition">{hero_title}</h1>
          <p class="mt-4 text-lg text-zinc-600 leading-relaxed">{hero_excerpt}</p>
          <div class="mt-6 flex items-center text-sm text-zinc-400"><span>Read Full Story &rarr;</span></div>
        </div>
      </a>
    </section>
    <hr class="border-zinc-200 mb-16">
    """

    # GRID DATA
    grid_html = ""
    for p in items[1:5]:
        title, cat, excerpt = get_article_info(p)
        img = p.with_suffix('.jpg').name
        path = f"articles/{p.parent.name}/{p.name}"
        grid_html += f"""
        <article class="group cursor-pointer flex flex-col md:flex-row gap-6">
          <div class="w-full md:w-1/3 overflow-hidden rounded-sm">
            <a href="{path}"><img src="articles/{p.parent.name}/{img}" class="h-48 w-full object-cover transition group-hover:scale-105"></a>
          </div>
          <div class="flex-1">
            <span class="text-[10px] font-bold uppercase tracking-widest text-zinc-400">{cat}</span>
            <h3 class="mt-2 font-serif text-2xl font-bold group-hover:text-zinc-600 transition"><a href="{path}">{title}</a></h3>
            <p class="mt-2 text-zinc-600 line-clamp-2">{excerpt}</p>
            <div class="mt-4 text-xs font-bold text-zinc-900 uppercase"><a href="{path}">Read Story &rarr;</a></div>
          </div>
        </article>
        """

    # SIDEBAR DATA
    sidebar_html = ""
    for i, p in enumerate(items[5:8]):
        title, cat, _ = get_article_info(p)
        path = f"articles/{p.parent.name}/{p.name}"
        sidebar_html += f"""
        <div class="flex gap-4 items-start">
          <span class="text-3xl font-serif text-zinc-200">0{i+1}</span>
          <div>
            <a href="{path}" class="font-bold hover:text-zinc-500 transition leading-snug block">{title}</a>
            <p class="text-xs text-zinc-400 mt-1 uppercase tracking-widest">{cat}</p>
          </div>
        </div>
        """

    (WEBSITE_DIR / "index.html").write_text(f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Pahayagang Palanyag</title>
  <link rel="icon" href="favicon.png">
  <script src="https://cdn.tailwindcss.com"></script>
  <link href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@700;900&family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
  <script>
    tailwind.config = {{
      theme: {{
        extend: {{
          fontFamily: {{ serif: ['"Playfair Display"', 'serif'], sans: ['Inter', 'sans-serif'] }}
        }}
      }}
    }}
  </script>
  <style>body {{ background-color: #fff; color: #18181b; }}</style>
</head>
<body class="font-sans antialiased">

  <nav class="sticky top-0 z-50 border-b border-zinc-200 bg-white/80 backdrop-blur-md">
    <div class="mx-auto flex max-w-7xl items-center justify-between px-6 py-4">
      <a href="index.html" class="flex items-center gap-3">
          <img src="PPICON.png" class="h-12 w-auto">
          <img src="PPTITLE.png" class="h-9 w-auto mt-1">
      </a>
      <div class="hidden space-x-8 text-sm font-medium uppercase tracking-widest md:flex">
        <a href="index.html" class="hover:text-zinc-500 transition">Home</a>
        {nav_links}
      </div>
    </div>
  </nav>

  <main class="mx-auto max-w-7xl px-6 py-12">
    {hero_html}
    <div class="grid grid-cols-1 gap-16 lg:grid-cols-12">
      <div class="lg:col-span-8">
        <h2 class="mb-8 font-serif text-3xl font-bold border-b border-zinc-900 pb-2">Latest News</h2>
        <div class="space-y-12">
          {grid_html}
        </div>
      </div>
      <aside class="lg:col-span-4">
        <div class="sticky top-24">
          <h2 class="border-b border-zinc-900 pb-2 text-xs font-black uppercase tracking-[0.2em]">Trending</h2>
          <div class="mt-6 space-y-8">
            {sidebar_html}
          </div>
          <div class="mt-16 rounded-sm bg-zinc-100 p-8">
            <h3 class="font-serif text-xl font-bold">The Morning Briefing</h3>
            <p class="mt-2 text-sm text-zinc-600">Get the most important stories delivered to your inbox every morning.</p>
            <input type="email" placeholder="email@example.com" class="mt-4 w-full border border-zinc-300 bg-white px-4 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-zinc-400">
            <button class="mt-4 w-full bg-zinc-900 py-2 text-sm font-medium text-white transition hover:bg-zinc-700">Join Newsletter</button>
          </div>
        </div>
      </aside>
    </div>
  </main>

  <footer class="mt-24 border-t border-zinc-200 bg-zinc-50 py-12">
    <div class="mx-auto max-w-7xl px-6 text-center">
      <div class="flex justify-center items-center gap-2 mb-4">
          <img src="PPICON.png" class="h-10 w-auto opacity-50 grayscale">
          <img src="PPTITLE.png" class="h-8 w-auto mt-1 opacity-50 grayscale">
      </div>
      <p class="mt-4 text-xs text-zinc-500 uppercase tracking-widest">© {datetime.datetime.now().year} Pahayagang Palanyag. All rights reserved.</p>
    </div>
  </footer>

</body>
</html>""")
    print("[WEB] ✅ Homepage updated with Tailwind layout")

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