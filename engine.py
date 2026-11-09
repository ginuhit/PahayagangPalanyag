# engine.py
import os, json, re, time, datetime, hashlib, shutil, random
from pathlib import Path
from urllib.parse import urljoin, quote
from bs4 import BeautifulSoup

import feedparser
import requests
import schedule
from PIL import Image, ImageDraw, ImageFont
import ollama

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
    MEMORY_FILE.write_text(json.dumps(memory[:10], indent=2))

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
        r = cffi_requests.get(url, impersonate="chrome120", timeout=15)
        soup = BeautifulSoup(r.text, 'html.parser')
        paragraphs = soup.find_all('p')
        text = "\n".join([p.get_text().strip() for p in paragraphs if len(p.get_text().strip()) > 40])
        return text[:4000] if text else "Full text unavailable."
    except Exception:
        return "Full text unavailable."

def get_cross_references(title):
    try:
        r = cffi_requests.get(f"https://html.duckduckgo.com/html/?q={quote(title)}", impersonate="chrome120", timeout=10)
        soup = BeautifulSoup(r.text, 'html.parser')
        snippets = soup.find_all('a', class_='result__snippet')
        return "\n".join([s.get_text().strip() for s in snippets[:3]])
    except Exception:
        return "Cross-references unavailable."

def check_social_trends():
    print("[SOCIAL] Scanning social media temperature...")
    try:
        query = "site:facebook.com OR site:x.com \"Paranaque\" news"
        r = cffi_requests.get(f"https://html.duckduckgo.com/html/?q={quote(query)}", impersonate="chrome120", timeout=10)
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
        r = cffi_requests.get("https://html.duckduckgo.com/html/?q=Paranaque+news+RSS", impersonate="chrome120", timeout=10)
        soup = BeautifulSoup(r.text, 'html.parser')
        links = soup.find_all('a', class_='result__url')
        
        sources = load_sources()
        existing_urls = [url for tier in sources.values() for url in tier]
        
        for link in links[:2]:
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
Your tone is straight, factual, evidence-based, educational, and elegantly written. No AI slop.

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

CRITICAL INSTRUCTION ON LOCAL RELEVANCE:
You are a Parañaque publication, but you must NOT force a Parañaque angle if it does not genuinely exist. 
- If the story is specifically about Parañaque, dive deep into local details.
- If the story is national or international, focus on the broader human, economic, or political impact. Do NOT shoehorn a local connection. It feels lazy and artificial.
- Only mention Parañaque if it is a factual, direct connection.

Write a SEPARATE 2-sentence social hook (Facebook caption). Include relevant hashtags. 
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
        context=context_str, social_trends=social_str, memory=memory_str or "None",
        cross_refs=cross_refs, source=article["source"], title=article["title"],
        tier=article["tier"], text=full_text[:3000], angle=angle
    )
    
    resp = ollama.chat(
        model=MODEL_NAME, 
        messages=[{"role": "user", "content": prompt}],
        format=SCHEMA, 
        options={"temperature": 0.4, "num_predict": 2048, "num_ctx": 8192}
    )
    
    raw_output = resp["message"]["content"].strip()
    if not raw_output:
        raise ValueError("Empty response from Ollama")
        
    try:
        return json.loads(raw_output)
    except json.JSONDecodeError:
        print(f"[AI] Failed to parse JSON. Raw output was:\n{raw_output}")
        raise

# ---------- IMAGE COMPOSER ----------
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
    img = Image.new("RGB", (1200, 630), (10, 25, 50))
    draw = ImageDraw.Draw(img)
    for y in range(630):
        r = int(10 + (30 - 10) * (y / 630))
        g = int(25 + (70 - 25) * (y / 630))
        b = int(50 + (150 - 50) * (y / 630))
        draw.line([(0, y), (1200, y)], fill=(r, g, b))
    img.save(save_path)
    return True

def compose_image(source_img_path, headline, credit, social_out_path, web_out_path):
    # 1. Base Image (Full size, not cropped)
    img = Image.open(source_img_path).convert("RGBA").resize((1200, 630))
    
    # 2. Draw Logo (With margin on right)
    logo_img = img.copy()
    draw = ImageDraw.Draw(logo_img)

    # Logo Box (Top right, with 20px margin)
    box_x1, box_y1 = 1000, 20
    box_x2, box_y2 = 1180, 140
    draw.rectangle([(box_x1, box_y1), (box_x2, box_y2)], fill=(255, 255, 255, 255))

    logo_path = ASSETS_DIR / "logo.png"
    if logo_path.exists():
        logo = Image.open(logo_path).convert("RGBA")
        bbox = logo.getbbox()
        if bbox:
            logo = logo.crop(bbox)
        logo.thumbnail((140, 100))
        logo_w, logo_h = logo.size
        pos_x = box_x1 + (180 - logo_w) // 2
        pos_y = box_y1 + (120 - logo_h) // 2
        logo_img.paste(logo, (pos_x, pos_y), logo)

    font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    try:
        font = ImageFont.truetype(font_path, 34)
        font_small = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 18)
    except:
        font = ImageFont.load_default()
        font_small = ImageFont.load_default()

    # --- A. WEBSITE IMAGE (Credit Line Only, No Headline) ---
    overlay_web = Image.new("RGBA", logo_img.size, (0, 0, 0, 0))
    draw_web = ImageDraw.Draw(overlay_web)
    
    # Small black box for credit line at bottom left
    draw_web.rectangle([(0, 590), (650, 630)], fill=(0, 0, 0, 180))
    draw_web.text((10, 600), credit[:90], font=font_small, fill=(200, 200, 200))
    
    final_web = Image.alpha_composite(logo_img, overlay_web).convert("RGB")
    final_web.save(web_out_path, quality=92)
    print(f"[IMG] Clean web image saved -> {web_out_path}")

    # --- B. SOCIAL IMAGE (Headline + Credit Line) ---
    overlay_social = Image.new("RGBA", logo_img.size, (0, 0, 0, 0))
    draw_social = ImageDraw.Draw(overlay_social)

    # Headline Wrapping
    words = headline.split()
    lines = []
    current_line = ""
    for word in words:
        test_line = f"{current_line} {word}".strip()
        if font.getlength(test_line) < 1100:
            current_line = test_line
        else:
            if current_line: lines.append(current_line)
            current_line = word
    if current_line: lines.append(current_line)

    if len(lines) > 3:
        lines = lines[:3]
        lines[-1] = lines[-1] + "..."

    draw_social.rectangle([(20, 510), (1180, 590)], fill=(0, 0, 0, 200))
    y_text = 520
    for line in lines:
        draw_social.text((40, y_text), line, font=font, fill=(255, 255, 255))
        y_text += 32 

    draw_social.text((20, 600), credit[:100], font=font_small, fill=(200, 200, 200))

    final_social = Image.alpha_composite(logo_img, overlay_social).convert("RGB")
    final_social.save(social_out_path, quality=92)
    print(f"[IMG] Social image with headline saved -> {social_out_path}")

# ---------- TELEGRAM APPROVAL ----------
def send_for_approval(image_path, source_img_path, article_data):
    headline = article_data["headline"]
    editorial = article_data["editorial"]
    social_caption = article_data["social_caption"]
    draft_id = hashlib.md5(headline.encode()).hexdigest()[:8]
    (DRAFTS_DIR / f"{draft_id}.json").write_text(json.dumps(article_data))

    keyboard = {"inline_keyboard": [[
        {"text": "✅ Approve", "callback_data": f"approve:{draft_id}"},
        {"text": "✏️ Edit Headline", "callback_data": f"edit:{draft_id}"},
        {"text": "❌ Reject", "callback_data": f"reject:{draft_id}"},
    ]]}

    def send_preview():
        with open(image_path, "rb") as f: # image_path will be draft_social.jpg
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
                    
                    if data.startswith("approve:"): return "approve"
                    if data.startswith("reject:"): return "reject"
                    if data.startswith("edit:"):
                        requests.post(f"{TELEGRAM_API}/sendMessage", data={"chat_id": TELEGRAM_CHAT_ID, "text": "Reply with the new headline."})
                
                elif "message" in update and "text" in update["message"]:
                    new_headline = update["message"]["text"]
                    article_data["headline"] = new_headline
                    compose_image(source_img_path, new_headline, article_data["image_credit"], image_path)
                    send_preview()
        except Exception:
            pass
        time.sleep(2)
    return None

# ---------- PUBLISHING ----------
def publish_to_facebook(image_path, caption):
    url = f"https://graph.facebook.com/v19.0/{FACEBOOK_PAGE_ID}/photos"
    with open(image_path, "rb") as f:
        r = requests.post(url, data={"message": caption, "access_token": FACEBOOK_PAGE_ACCESS_TOKEN}, files={"source": f})
    print(f"[FB] Status: {r.status_code}")
    if r.status_code != 200:
        print(f"[FB] Error: {r.text}")

def adsense_head():
    client = getattr(__import__("secrets"), "ADSENSE_CLIENT_ID", "")
    if not client: return ""
    return f'<script async src="https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js?client={client}" crossorigin="anonymous"></script>'

def publish_to_website(article_data, image_filename):
    slug = re.sub(r"[^a-z0-9]+", "-", article_data["headline"].lower()).strip("-")[:60]
    new_image_name = f"{slug}.jpg"
    paragraphs = ''.join(f'<p>{p}</p>' for p in article_data['editorial'].split(chr(10)) if p.strip())
    
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{article_data['headline']} | Pahayagang Palanyag</title>
    <link rel="stylesheet" href="../style.css">
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@700;900&family=Inter:wght@400;600&display=swap" rel="stylesheet">
    {adsense_head()}
</head>
<body>
    <header class="site-header">
        <div class="container"><a href="../index.html" class="logo">Pahayagang Palanyag</a></div>
    </header>
    
    <main class="container">
        <header class="article-header">
            <h1 class="article-title">{article_data['headline']}</h1>
            <p class="credit">{article_data['image_credit']}</p>
        </header>
        
        <img src="{new_image_name}" alt="{article_data['headline']}" class="hero-image-article">
        
        <article class="article-content">
            {paragraphs}
        </article>
    </main>
    
    <footer class="site-footer">
        <div class="container"><p>&copy; {datetime.datetime.now().year} Pahayagang Palanyag.</p></div>
    </footer>
</body>
</html>"""
    
    (ARTICLES_DIR / f"{slug}.html").write_text(html)
    # Copy the CLEAN web image (not the one with the text)
    shutil.copy(DRAFTS_DIR / image_filename, ARTICLES_DIR / new_image_name)
    save_to_memory(article_data['headline'], article_data['editorial'])

def update_homepage():
    items = sorted(ARTICLES_DIR.glob("*.html"), key=os.path.getmtime, reverse=True)
    if not items: return

    featured = items[0]
    featured_title = featured.stem.replace("-", " ").title()
    featured_img_name = featured.with_suffix('.jpg').name
    featured_img = f'<img src="articles/{featured_img_name}" alt="{featured_title}">' if (ARTICLES_DIR / featured_img_name).exists() else ''
    
    hero_html = f"""
    <div class="hero-section">
        <div class="hero-image-container">
            <a href="articles/{featured.name}">{featured_img}</a>
        </div>
        <div class="hero-content">
            <h1><a href="articles/{featured.name}" style="text-decoration:none; color:inherit;">{featured_title}</a></h1>
            <a href="articles/{featured.name}" class="read-more">Read Full Story &rarr;</a>
        </div>
    </div>
    """

    cards = ""
    for p in items[1:13]:
        title = p.stem.replace("-", " ").title()
        img_name = p.with_suffix('.jpg').name
        img_tag = f'<img src="articles/{img_name}" alt="{title}">' if (ARTICLES_DIR / img_name).exists() else ''
        cards += f"""
        <a href="articles/{p.name}" class="news-card">
            {img_tag}
            <h2>{title}</h2>
            <span class="read-more">Read Story &rarr;</span>
        </a>
        """
        
    (WEBSITE_DIR / "index.html").write_text(f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Pahayagang Palanyag | Parañaque News</title>
    <link rel="stylesheet" href="style.css">
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@700;900&family=Inter:wght@400;600&display=swap" rel="stylesheet">
    {adsense_head()}
</head>
<body>
    <header class="site-header">
        <div class="container"><a href="index.html" class="logo">Pahayagang Palanyag</a></div>
    </header>
    
    <nav class="site-nav">
        <a href="#">News</a><a href="#">Politics</a><a href="#">Metro Manila</a><a href="#">Opinion</a>
    </nav>

    <main class="container">
        {hero_html}
        <h2 class="section-title">Latest News</h2>
        <div class="news-grid">
            {cards}
        </div>
    </main>
    
    <footer class="site-footer">
        <div class="container"><p>&copy; {datetime.datetime.now().year} Pahayagang Palanyag.</p></div>
    </footer>
</body>
</html>""")
    print("[WEB] ✅ Homepage updated with clean Magazine layout")

# ---------- MAIN CYCLE ----------
def full_cycle():
    print(f"\n[{datetime.datetime.now()}] cycle start")
    discover_new_sources()
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
        Image.new("RGB", (1200, 630), (20, 40, 80)).save(source_img)

    # Generate the two distinct images
    social_img = DRAFTS_DIR / "draft_social.jpg"
    web_img = DRAFTS_DIR / "draft_web.jpg"
    compose_image(source_img, data["headline"], data["image_credit"], social_img, web_img)

    # Telegram sees the social image (with text)
    action = send_for_approval(social_img, source_img, data)

    if action == "approve":
        slug = re.sub(r"[^a-z0-9]+", "-", data["headline"].lower()).strip("-")[:60]
        article_url = f"https://pahayagangpalanyag.pages.dev/articles/{slug}.html"
        caption_with_link = f"{data['social_caption']}\n\nRead the full story here: {article_url}"
        
        print("[PUBLISH] Posting to Facebook...")
        publish_to_facebook(social_img, caption_with_link)
        
        print("[PUBLISH] Writing to Website...")
        # Pass the web_img filename so the website gets the clean version
        publish_to_website(data, "draft_web.jpg")
        update_homepage()
        
        print("[GIT] Pushing to Cloudflare...")
        os.chdir(WEBSITE_DIR)
        import subprocess
        subprocess.run(["git", "add", "."], check=False)
        subprocess.run(["git", "commit", "-m", "Auto-publish new article"], check=False)
        result = subprocess.run(["git", "push"], capture_output=True, text=True)
        print(f"[GIT] Push result: {result.stderr or result.stdout}")
        
        os.chdir(BASE_DIR)
        print("[PUBLISH] Done. Live on Facebook and Website.")
    else:
        print("[PUBLISH] Aborted by user.")

# ---------- SCHEDULE ----------
if __name__ == "__main__":
    print("Engine running. Deep-Research mode active.")
    full_cycle()
    schedule.every(4).hours.do(full_cycle)
    while True:
        schedule.run_pending()
        time.sleep(60)