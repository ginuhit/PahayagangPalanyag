import os, json, re, time, datetime, hashlib, shutil, random, textwrap
from pathlib import Path
from urllib.parse import urljoin, quote
from bs4 import BeautifulSoup

import feedparser
import requests  # Keep this for Telegram and Facebook
import schedule
from PIL import Image, ImageDraw, ImageFont
import ollama

# This is the bypass library
from curl_cffi import requests as cffi_requests 

from secrets import *

# ---------- CONFIGURATION & FOLDERS ----------
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

# ---------- LOCAL CONTEXT & MEMORY ----------
def load_context():
    if not CONTEXT_FILE.exists():
        default_context = (
            "- 'Palanyag' is the old name for Parañaque.\n"
            "- Local officials: Mayor Eric Olivarez, Rep. Edwin Olivarez.\n"
            "- Local issues: flooding on Sucat Road, traffic on Dr. A. Santos Ave.\n"
            "- Tone: Always pivot national issues to the local barangay level."
        )
        CONTEXT_FILE.write_text(default_context)
        return default_context
    return CONTEXT_FILE.read_text()

def load_memory():
    if MEMORY_FILE.exists():
        return json.loads(MEMORY_FILE.read_text())
    return []

def save_to_memory(headline, summary):
    memory = load_memory()
    memory.insert(0, {"date": str(datetime.date.today()), "headline": headline, "summary": summary[:200]})
    MEMORY_FILE.write_text(json.dumps(memory[:10], indent=2)) # Keep last 10

# ---------- TIERED SOURCES ----------
DEFAULT_SOURCES = {
    "tier_1_gov": ["https://www.paranaquecity.gov.ph/feed/"],
    "tier_2_news": [
        "https://www.inquirer.net/fullfeed",
        "https://www.philstar.com/rss/nation",
        "https://www.rappler.com/feed/"
    ],
    "tier_3_social": []
}

def load_sources():
    if SOURCES_FILE.exists():
        return json.loads(SOURCES_FILE.read_text())
    else:
        SOURCES_FILE.write_text(json.dumps(DEFAULT_SOURCES, indent=2))
        return DEFAULT_SOURCES

def save_sources(sources):
    SOURCES_FILE.write_text(json.dumps(sources, indent=2))

# ---------- RESEARCH & SCRAPING ----------
def get_full_article(url):
    try:
        headers = {'User-Agent': 'Mozilla/5.0'}
        r = requests.get(url, headers=headers, timeout=15)
        soup = BeautifulSoup(r.text, 'html.parser')
        paragraphs = soup.find_all('p')
        text = "\n".join([p.get_text().strip() for p in paragraphs if len(p.get_text().strip()) > 40])
        return text[:4000] if text else "Full text unavailable."
    except Exception:
        return "Full text unavailable."

def get_cross_references(title):
    try:
        headers = {'User-Agent': 'Mozilla/5.0'}
        r = requests.get(f"https://html.duckduckgo.com/html/?q={quote(title)}", headers=headers, timeout=10)
        soup = BeautifulSoup(r.text, 'html.parser')
        snippets = soup.find_all('a', class_='result__snippet')
        return "\n".join([s.get_text().strip() for s in snippets[:3]])
    except Exception:
        return "Cross-references unavailable."

def check_social_trends():
    print("[SOCIAL] Scanning social media temperature...")
    try:
        headers = {'User-Agent': 'Mozilla/5.0'}
        query = "site:facebook.com OR site:x.com \"Paranaque\" news"
        r = requests.get(f"https://html.duckduckgo.com/html/?q={quote(query)}", headers=headers, timeout=10)
        soup = BeautifulSoup(r.text, 'html.parser')
        results = soup.find_all('a', class_='result__snippet')
        
        if len(results) >= 5:
            print(f"[SOCIAL] Trend detected! ({len(results)} posts).")
            snippets = "\n".join([s.get_text().strip() for s in results[:5]])
            return f"TRENDING ON SOCIAL MEDIA:\n{snippets}\n\nACTION: Trace this claim to an official source."
        return "No major social trends detected."
    except Exception:
        return "Social check failed."

def discover_new_sources():
    print("[DISCOVERY] Searching for new credible sources...")
    try:
        headers = {'User-Agent': 'Mozilla/5.0'}
        r = requests.get("https://html.duckduckgo.com/html/?q=Paranaque+news+RSS", headers=headers, timeout=10)
        soup = BeautifulSoup(r.text, 'html.parser')
        links = soup.find_all('a', class_='result__url')
        
        sources = load_sources()
        existing_urls = [url for tier in sources.values() for url in tier]
        
        for link in links[:2]: # Check top 2 results to avoid overloading
            domain = link.get_text().strip()
            if domain and not any(domain in url for url in existing_urls):
                prompt = f"Is '{domain}' a reputable news website or official government site in the Philippines? Reply with ONLY a number from 0.0 to 1.0."
                resp = ollama.chat(model=MODEL_NAME, messages=[{"role": "user", "content": prompt}])
                try:
                    score = float(re.search(r"0\.\d+|1\.0|1|0", resp['message']['content']).group())
                    if score >= 0.7:
                        print(f"[DISCOVERY] Added new source: {domain}")
                        sources["tier_2_news"].append(f"https://{domain}/feed/")
                        save_sources(sources)
                except:
                    pass
    except Exception as e:
        print(f"[DISCOVERY] Search failed: {e}")

def fetch_articles(limit_per_tier=2):
    articles = []
    sources = load_sources()
    for tier, urls in sources.items():
        if tier == "tier_3_social": continue
        for url in urls:
            try:
                feed = feedparser.parse(url)
                for entry in feed.entries[:limit_per_tier]:
                    articles.append({
                        "title": entry.title, "link": entry.link,
                        "summary": entry.get("summary", ""),
                        "source": feed.feed.get("title", url), "tier": tier
                    })
            except: pass
    articles.sort(key=lambda x: x["tier"])
    return articles

# ---------- AI WRITER ----------
SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string"}, "editorial": {"type": "string"},
        "social_caption": {"type": "string"}, "credibility": {"type": "number"},
        "image_credit": {"type": "string"},
    },
    "required": ["headline", "editorial", "social_caption", "credibility", "image_credit"],
}

ANGLES = [
    "Focus on the everyday commuter and working class.",
    "Focus on long-term economic impact on local businesses.",
    "Focus on the historical context in Parañaque.",
    "Focus on youth and future generations.",
    "Focus on practical, granular details people need."
]

PROMPT_TEMPLATE = """You are the Editor-in-Chief of Pahayagang Palanyag.
Your tone is straight, factual, evidence-based, educational, and an elegant writer.

### LOCAL CONTEXT
{context}

### SOCIAL TEMPERATURE CHECK
{social_trends}

### MEMORY (Past Context)
{memory}

### CROSS-REFERENCES
{cross_refs}

### SOURCE ARTICLE
Source: {source} (Tier: {tier})
Title: {title}
Text:


### TASK
Write a 4-6 paragraph editorial answering: What happened? Why does it matter? What is the hidden context? 
Adopt this lens: **{angle}**
Write a SEPARATE 2-sentence social hook (Facebook caption).
Give a credibility score (0.0 to 1.0).
Give an image credit line.

You MUST return ONLY a valid JSON object matching this exact schema: {schema}
"""

def ai_write(article):
    full_text = get_full_article(article["link"]) or article["summary"]
    cross_refs = get_cross_references(article["title"])
    memory_str = "\n".join([f"- {m['date']}: {m['headline']}" for m in load_memory()])
    context_str = load_context()
    social_str = check_social_trends()
    angle = random.choice(ANGLES)
    
    print(f"[AI] Writing with angle: {angle} | Tier: {article['tier']}")
    
    prompt = PROMPT_TEMPLATE.format(
        context=context_str,
        social_trends=social_str,
        memory=memory_str or "None",
        cross_refs=cross_refs, 
        source=article["source"], 
        title=article["title"],
        tier=article["tier"], 
        text=full_text[:3000], 
        angle=angle,
        schema=json.dumps(SCHEMA) # Pass the schema directly into the text
    )
    
    resp = ollama.chat(
        model=MODEL_NAME, 
        messages=[{"role": "user", "content": prompt}],
        format=SCHEMA, 
        options={
            "temperature": 0.4,      
            "num_predict": 2048,
            "num_ctx": 8192         
        }
    )
    
    raw_output = resp["message"]["content"].strip()
    
    if not raw_output:
        print(f"[AI] Error: The model returned an empty response.")
        print(f"Prompt length was: {len(prompt)} characters.")
        raise ValueError("Empty response from Ollama")
        
    try:
        return json.loads(raw_output)
    except json.JSONDecodeError:
        print(f"[AI] Failed to parse JSON. Raw output was:\n{raw_output}")
        raise

# ---------- IMAGE COMPOSER ----------
from curl_cffi import requests as cffi_requests

def download_image(article_url, save_path, headline):
    print(f"[IMG] Attempting to scrape: {article_url}")
    try:
        r = cffi_requests.get(article_url, impersonate="chrome120", timeout=15)
        soup = BeautifulSoup(r.text, 'html.parser')
        
        img_url = None
        for tag in [{"property": "og:image"}, {"name": "twitter:image"}, {"itemprop": "image"}]:
            found = soup.find("meta", attrs=tag)
            if found and found.get("content"):
                img_url = found["content"]
                break
                
        if img_url:
            img_url = urljoin(article_url, img_url)
            img_resp = cffi_requests.get(img_url, impersonate="chrome120", timeout=15)
            if 'image' in img_resp.headers.get('Content-Type', '').lower():
                with open(save_path, 'wb') as f:
                    f.write(img_resp.content)
                print(f"[IMG] ✅ Successfully downloaded image.")
                return True
    except Exception as e:
        print(f"[IMG] ❌ Scrape failed: {e}")

    print("[IMG] 🎨 Generating fallback gradient image...")
    # Fallback: Create a nice gradient
    img = Image.new("RGB", (1200, 630), (10, 25, 50))
    draw = ImageDraw.Draw(img)
    for y in range(630):
        r = int(10 + (30 - 10) * (y / 630))
        g = int(25 + (70 - 25) * (y / 630))
        b = int(50 + (150 - 50) * (y / 630))
        draw.line([(0, y), (1200, y)], fill=(r, g, b))
    img.save(save_path)
    return True

import textwrap

def compose_image(source_img_path, headline, credit, out_path):
    img = Image.open(source_img_path).convert("RGBA").resize((1200, 630))
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    # Perfectly sized box: 160px wide, 60px tall (Top Right)
    box_x1, box_y1 = 1040, 0
    box_x2, box_y2 = 1200, 60
    draw.rectangle([(box_x1, box_y1), (box_x2, box_y2)], fill=(255, 255, 255, 160)) 
    
    logo_path = ASSETS_DIR / "logo.png"
    if logo_path.exists():
        logo = Image.open(logo_path).convert("RGBA")
        logo.thumbnail((140, 50)) # Fits inside the box with 10px padding
        
        # Calculate center position of the box
        logo_w, logo_h = logo.size
        pos_x = box_x1 + (160 - logo_w) // 2
        pos_y = box_y1 + (60 - logo_h) // 2
        
        img.paste(logo, (pos_x, pos_y), logo)

    font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    try:
        font = ImageFont.truetype(font_path, 34)
        lines = textwrap.wrap(headline, width=60) 
        if len(lines) > 3:
            lines = lines[:3]
            lines[-1] = lines[-1] + "..."
        font_small = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 18)
    except:
        font = ImageFont.load_default()
        font_small = ImageFont.load_default()
        lines = [headline[:70]]

    draw.rectangle([(20, 500), (1180, 590)], fill=(0, 0, 0, 200))
    y_text = 515
    for line in lines:
        draw.text((40, y_text), line, font=font, fill=(255, 255, 255))
        y_text += 35 

    draw.text((20, 600), credit[:100], font=font_small, fill=(200, 200, 200))

    Image.alpha_composite(img, overlay).convert("RGB").save(out_path, quality=92)
    print(f"[IMG] Composed -> {out_path}")

# ---------- TELEGRAM ----------
def send_for_approval(image_path, source_img_path, article_data):
    headline = article_data["headline"]
    editorial = article_data["editorial"]
    social_caption = article_data["social_caption"]
    draft_id = hashlib.md5(headline.encode()).hexdigest()[:8]
    
    keyboard = {"inline_keyboard": [[
        {"text": "✅ Approve", "callback_data": f"approve:{draft_id}"},
        {"text": "✏️ Edit Headline", "callback_data": f"edit:{draft_id}"},
        {"text": "❌ Reject", "callback_data": f"reject:{draft_id}"},
    ]]}

    def send_preview():
        with open(image_path, "rb") as f:
            requests.post(f"{TELEGRAM_API}/sendPhoto", data={"chat_id": TELEGRAM_CHAT_ID}, files={"photo": f})
        
        full_text = f"📰 *HEADLINE:*\n{headline}\n\n📱 *FACEBOOK:*\n{social_caption}\n\n📝 *EDITORIAL:*\n{editorial}\n\n(Actions below)"
        requests.post(f"{TELEGRAM_API}/sendMessage", data={
            "chat_id": TELEGRAM_CHAT_ID, "text": full_text[:4000],
            "reply_markup": json.dumps(keyboard), "disable_web_page_preview": True
        })

    send_preview()
    print("[TG] Waiting for your click on Telegram...")
    
    offset = None
    deadline = time.time() + 3600 
    while time.time() < deadline:
        try:
            params = {"timeout": 25}
            if offset: params["offset"] = offset
            r = requests.get(f"{TELEGRAM_API}/getUpdates", params=params, timeout=35)
            
            for update in r.json().get("result", []):
                offset = update["update_id"] + 1
                
                if "callback_query" in update:
                    cq = update["callback_query"]
                    data = cq.get("data", "")
                    requests.post(f"{TELEGRAM_API}/answerCallbackQuery", data={"callback_query_id": cq["id"]})
                    
                    if data.startswith("approve:"): 
                        print("[TG] ✅ APPROVE clicked!")
                        return "approve"
                    if data.startswith("reject:"): 
                        print("[TG] ❌ REJECT clicked!")
                        return "reject"
                    if data.startswith("edit:"):
                        print("[TG] ✏️ EDIT clicked. Waiting for new text...")
                        requests.post(f"{TELEGRAM_API}/sendMessage", data={"chat_id": TELEGRAM_CHAT_ID, "text": "Reply with the new headline."})
                
                elif "message" in update and "text" in update["message"]:
                    new_headline = update["message"]["text"]
                    article_data["headline"] = new_headline
                    compose_image(source_img_path, new_headline, article_data["image_credit"], image_path)
                    print("[TG] Headline updated and image re-composed.")
                    send_preview()
        except Exception as e:
            pass
        time.sleep(2)
    return None

def full_cycle():
    print(f"\n[{datetime.datetime.now()}] Cycle start")
    # discover_new_sources() # Temporarily commented out to speed up testing
    articles = fetch_articles(2)
    if not articles: return
    
    art = articles[0]
    print(f"[AI] Analyzing: {art['title'][:60]}")
    try: 
        data = ai_write(art)
    except Exception as e:
        print(f"[AI] failed: {e}"); return

    source_img = ASSETS_DIR / "source_image.jpg"
    if not download_image(art["link"], source_img, data["headline"]):
        print("[IMG] Using placeholder blue box.")
        Image.new("RGB", (1200, 630), (20, 40, 80)).save(source_img)

    img_out = DRAFTS_DIR / "draft.jpg"
    compose_image(source_img, data["headline"], data["image_credit"], img_out)

    action = send_for_approval(img_out, source_img, data)

    if action == "approve":
        print("[PUBLISH] Posting to Facebook...")
        publish_to_facebook(img_out, data["social_caption"])
        
        print("[PUBLISH] Writing to Website...")
        publish_to_website(data, "draft.jpg")
        update_homepage()
        
        print("[GIT] Pushing to Cloudflare...")
        os.chdir(WEBSITE_DIR)
        # Using subprocess to prevent Git from hanging on password prompts
        import subprocess
        subprocess.run(["git", "add", "."], check=False)
        subprocess.run(["git", "commit", "-m", "Auto-publish new article"], check=False)
        result = subprocess.run(["git", "push"], capture_output=True, text=True)
        print(f"[GIT] Push result: {result.stderr or result.stdout}")
        
        os.chdir(BASE_DIR)
        print("[PUBLISH] Done. Live on Facebook and Website.")
    else:
        print("[PUBLISH] Aborted by user.")

# ---------- PUBLISHING ----------
def publish_to_facebook(image_path, caption):
    print(f"[FB] Attempting to post to Page ID: {FACEBOOK_PAGE_ID}")
    url = f"https://graph.facebook.com/v19.0/{FACEBOOK_PAGE_ID}/photos"
    with open(image_path, "rb") as f:
        r = requests.post(url, data={
            "message": caption, 
            "access_token": FACEBOOK_PAGE_ACCESS_TOKEN
        }, files={"source": f})
    
    if r.status_code == 200:
        print(f"[FB] ✅ Successfully published to Facebook!")
    else:
        print(f"[FB] ❌ FAILED with status {r.status_code}")
        print(f"[FB] Facebook Error Details: {r.text}")

def adsense_head():
    client = getattr(__import__("secrets"), "ADSENSE_CLIENT_ID", "")
    if not client: return ""
    return f'<script async src="https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js?client={client}" crossorigin="anonymous"></script>'

def publish_to_website(article_data, image_filename):
    slug = re.sub(r"[^a-z0-9]+", "-", article_data["headline"].lower()).strip("-")[:60]
    new_image_name = f"{slug}.jpg"
    
    # Clean paragraph formatting
    paragraphs = ''.join(f'<p>{p}</p>' for p in article_data['editorial'].split(chr(10)) if p.strip())
    
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{article_data['headline']} | Pahayagang Palanyag</title>
    <link rel="stylesheet" href="../style.css">
    {adsense_head()}
</head>
<body>
    <header class="site-header">
        <div class="container">
            <a href="../index.html" class="logo">Pahayagang Palanyag</a>
        </div>
    </header>
    
    <main class="container">
        <header class="article-header">
            <h1 class="article-title">{article_data['headline']}</h1>
            <p class="credit">{article_data['image_credit']}</p>
        </header>
        
        <img src="{new_image_name}" alt="{article_data['headline']}" class="hero-image">
        
        <article class="article-content">
            {paragraphs}
        </article>
    </main>
    
    <footer class="site-footer">
        <div class="container">
            <p>&copy; {datetime.datetime.now().year} Pahayagang Palanyag. All rights reserved.</p>
        </div>
    </footer>
</body>
</html>"""
    
    (ARTICLES_DIR / f"{slug}.html").write_text(html)
    shutil.copy(DRAFTS_DIR / image_filename, ARTICLES_DIR / new_image_name)
    save_to_memory(article_data['headline'], article_data['editorial'])
    print(f"[WEB] Wrote articles/{slug}.html and {new_image_name}")

def update_homepage():
    items = sorted(ARTICLES_DIR.glob("*.html"), key=os.path.getmtime, reverse=True)
    
    if not items:
        (WEBSITE_DIR / "index.html").write_text("<!DOCTYPE html><html><body><h1>No articles yet.</h1></body></html>")
        return

    # 1. Get the newest article for the "Featured" slot
    featured = items[0]
    featured_title = featured.stem.replace("-", " ").title()
    featured_img_name = featured.with_suffix('.jpg').name
    featured_img = f'<img src="articles/{featured_img_name}" alt="{featured_title}">' if (ARTICLES_DIR / featured_img_name).exists() else ''
    
    featured_html = f"""
    <div style="margin-bottom: 50px;">
        <h2 class="section-title">Top Story</h2>
        <a href="articles/{featured.name}" class="news-card" style="flex-direction: row; align-items: center; gap: 30px; padding: 20px;">
            <div style="flex: 1; min-width: 300px;">
                {featured_img.replace('height: 200px;', 'height: 300px;')}
            </div>
            <div style="flex: 1;">
                <h2 style="font-size: 2.2rem; margin-bottom: 15px;">{featured_title}</h2>
                <span class="read-more">Read Full Story &rarr;</span>
            </div>
        </a>
    </div>
    """

    # 2. Loop through the rest for the grid
    cards = ""
    for p in items[1:20]:
        title = p.stem.replace("-", " ").title()
        img_name = p.with_suffix('.jpg').name
        img_tag = f'<img src="articles/{img_name}" alt="{title}">' if (ARTICLES_DIR / img_name).exists() else ''
        
        cards += f"""
        <a href="articles/{p.name}" class="news-card">
            {img_tag}
            <div class="card-content">
                <h2>{title}</h2>
                <span class="read-more">Read Story &rarr;</span>
            </div>
        </a>
        """
        
    (WEBSITE_DIR / "index.html").write_text(f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Pahayagang Palanyag | Parañaque News</title>
    <link rel="stylesheet" href="style.css">
    {adsense_head()}
</head>
<body>
    <header class="site-header">
        <div class="container">
            <a href="index.html" class="logo">Pahayagang Palanyag</a>
        </div>
    </header>
    
    <main class="container">
        {featured_html}
        <h2 class="section-title">Latest News</h2>
        <div class="news-grid">
            {cards}
        </div>
    </main>
    
    <footer class="site-footer">
        <div class="container">
            <p>&copy; {datetime.datetime.now().year} Pahayagang Palanyag.</p>
        </div>
    </footer>
</body>
</html>""")
    print("[WEB] Homepage rebuilt with a Featured Story and Grid layout")

def update_homepage():
    items = sorted(ARTICLES_DIR.glob("*.html"), key=os.path.getmtime, reverse=True)
    
    if not items:
        print("[WEB] No articles found.")
        return

    # Featured Story (Newest)
    featured = items[0]
    featured_title = featured.stem.replace("-", " ").title()
    featured_img_name = featured.with_suffix('.jpg').name
    featured_img = f'<img src="articles/{featured_img_name}" alt="{featured_title}" style="width:100%; height:300px; object-fit:cover;">' if (ARTICLES_DIR / featured_img_name).exists() else ''
    
    featured_html = f"""
    <div style="margin-bottom: 50px;">
        <h2 class="section-title">Top Story</h2>
        <a href="articles/{featured.name}" class="news-card" style="flex-direction: row; align-items: center; gap: 30px; padding: 20px; display: flex;">
            <div style="flex: 1; min-width: 300px;">
                {featured_img}
            </div>
            <div style="flex: 1;">
                <h2 style="font-size: 2.2rem; margin-bottom: 15px;">{featured_title}</h2>
                <span class="read-more">Read Full Story &rarr;</span>
            </div>
        </a>
    </div>
    """

    # Grid for the rest
    cards = ""
    for p in items[1:20]:
        title = p.stem.replace("-", " ").title()
        img_name = p.with_suffix('.jpg').name
        img_tag = f'<img src="articles/{img_name}" alt="{title}">' if (ARTICLES_DIR / img_name).exists() else ''
        
        cards += f"""
        <a href="articles/{p.name}" class="news-card">
            {img_tag}
            <div class="card-content">
                <h2>{title}</h2>
                <span class="read-more">Read Story &rarr;</span>
            </div>
        </a>
        """
        
    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Pahayagang Palanyag | Parañaque News</title>
    <link rel="stylesheet" href="style.css">
    {adsense_head()}
</head>
<body>
    <header class="site-header">
        <div class="container">
            <a href="index.html" class="logo">Pahayagang Palanyag</a>
        </div>
    </header>
    
    <main class="container">
        {featured_html}
        <h2 class="section-title">Latest News</h2>
        <div class="news-grid">
            {cards}
        </div>
    </main>
    
    <footer class="site-footer">
        <div class="container">
            <p>&copy; {datetime.datetime.now().year} Pahayagang Palanyag.</p>
        </div>
    </footer>
</body>
</html>"""
    
    # Force write to the file
    index_path = WEBSITE_DIR / "index.html"
    index_path.write_text(html_content, encoding='utf-8')
    print(f"[WEB] ✅ Successfully overwrote {index_path}")

# ---------- MAIN CYCLE ----------
def full_cycle():
    print(f"\n[{datetime.datetime.now()}] cycle start")
    discover_new_sources()
    articles = fetch_articles(2)
    if not articles: return
    
    art = articles[0]
    print(f"[AI] Writing: {art['title'][:60]}")
    try: data = ai_write(art)
    except Exception as e:
        print(f"[AI] failed: {e}"); return

    source_img = ASSETS_DIR / "source_image.jpg"
    if not download_image(art["link"], source_img, data["headline"]):
        Image.new("RGB", (1200, 630), (20, 40, 80)).save(source_img)

    img_out = DRAFTS_DIR / "draft.jpg"
    compose_image(source_img, data["headline"], data["image_credit"], img_out)

    action = send_for_approval(img_out, source_img, data)

    if action == "approve":
        # Create the URL based on the headline
        slug = re.sub(r"[^a-z0-9]+", "-", data["headline"].lower()).strip("-")[:60]
        article_url = f"https://pahayagang.palanyag.workers.dev/articles/{slug}.html"
        
        # Add the link to the caption
        caption_with_link = f"{data['social_caption']}\n\nRead the full story here: {article_url}"
        
        print("[PUBLISH] Posting to Facebook...")
        publish_to_facebook(img_out, caption_with_link)
        
        print("[PUBLISH] Writing to Website...")
        publish_to_website(data, "draft.jpg")
        update_homepage()

if __name__ == "__main__":
    print("Engine running. Deep-Research mode active.")
    full_cycle()
    schedule.every(4).hours.do(full_cycle)
    while True:
        schedule.run_pending()
        time.sleep(60)