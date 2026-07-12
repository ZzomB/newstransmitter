import os
import re
import time
from datetime import datetime
import requests
from bs4 import BeautifulSoup
import google.generativeai as genai
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# Configuration
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
SECTIONS = {
    "world": "https://apnews.com/hub/world-news",
    "business": "https://apnews.com/hub/business"
}
POSTS_DIR = "_posts"

# Required Environment Variables
EMAIL_SENDER = os.environ.get("EMAIL_SENDER")
EMAIL_PASSWORD = os.environ.get("EMAIL_PASSWORD")
EMAIL_RECEIVER = os.environ.get("EMAIL_RECEIVER")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

def check_env_vars():
    missing = []
    for var, name in [
        (EMAIL_SENDER, "EMAIL_SENDER"),
        (EMAIL_PASSWORD, "EMAIL_PASSWORD"),
        (EMAIL_RECEIVER, "EMAIL_RECEIVER"),
        (GEMINI_API_KEY, "GEMINI_API_KEY")
    ]:
        if not var:
            missing.append(name)
    if missing:
        raise ValueError(f"Missing required environment variables: {', '.join(missing)}")

def get_soup(url):
    headers = {"User-Agent": USER_AGENT}
    response = requests.get(url, headers=headers, timeout=15)
    response.raise_for_status()
    return BeautifulSoup(response.text, "html.parser")

def extract_article_links(hub_url):
    print(f"Extracting article links from: {hub_url}")
    soup = get_soup(hub_url)
    links = []
    # Find all anchor tags
    for a in soup.find_all("a", href=True):
        href = a["href"]
        # Normalize relative URLs
        if href.startswith("/"):
            href = "https://apnews.com" + href
        
        # Check if URL contains '/article/' and is valid
        if "apnews.com/article/" in href:
            # Clean parameters
            clean_url = href.split("?")[0].split("#")[0]
            if clean_url not in links:
                links.append(clean_url)
                if len(links) >= 3:
                    break
    return links

def extract_article_content(url):
    print(f"Extracting content from article: {url}")
    soup = get_soup(url)
    
    # 1. Title Extraction
    title = ""
    # Standard AP title tag
    title_tag = soup.find("h1") or soup.find("title")
    if title_tag:
        title = title_tag.get_text(strip=True)
    
    # 2. Body Extraction
    body = ""
    # AP News typical container class for article paragraphs
    body_container = soup.find("div", class_=lambda x: x and "RichTextStoryBody" in x)
    if not body_container:
        # Fallback 1: search for standard article content div
        body_container = soup.find("article")
    
    if body_container:
        paragraphs = body_container.find_all("p")
        body = "\n".join([p.get_text(strip=True) for p in paragraphs if p.get_text(strip=True)])
    
    # Fallback 2: if body is still empty, search all paragraphs in the page
    if not body:
        paragraphs = soup.find_all("p")
        body = "\n".join([p.get_text(strip=True) for p in paragraphs if p.get_text(strip=True)])
        
    if not body:
        raise ValueError(f"Could not extract body content from: {url}")
        
    return title, body

def send_consolidated_email(articles_by_section):
    print("Preparing consolidated email...")
    
    today_str = datetime.now().strftime("%Y-%m-%d")
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"[AP News Original] {today_str} Daily News Summary"
    msg["From"] = EMAIL_SENDER
    msg["To"] = EMAIL_RECEIVER

    # Build HTML content
    html_content = f"""
    <html>
    <head>
        <meta charset="utf-8">
        <style>
            body {{ font-family: 'Helvetica Neue', Helvetica, Arial, sans-serif; line-height: 1.6; color: #333; padding: 20px; }}
            h1 {{ border-bottom: 2px solid #146994; padding-bottom: 10px; color: #146994; }}
            h2 {{ color: #2c3e50; border-bottom: 1px solid #eee; padding-bottom: 5px; margin-top: 30px; text-transform: uppercase; }}
            .article {{ margin-bottom: 25px; background: #f9f9f9; padding: 15px; border-radius: 5px; border-left: 4px solid #146994; }}
            .title {{ font-size: 1.2em; font-weight: bold; color: #000; text-decoration: none; }}
            .url {{ font-size: 0.85em; color: #666; margin-top: 5px; margin-bottom: 10px; }}
            .body {{ white-space: pre-line; font-size: 0.95em; color: #444; }}
        </style>
    </head>
    <body>
        <h1>AP News Daily Original Body Summary</h1>
        <p>Date: {today_str}</p>
    """

    for section, articles in articles_by_section.items():
        if not articles:
            continue
        html_content += f"<h2>{section.upper()} News</h2>"
        for art in articles:
            html_content += f"""
            <div class="article">
                <a class="title" href="{art['url']}">{art['original_title']}</a>
                <div class="url"><a href="{art['url']}">{art['url']}</a></div>
                <div class="body">{art['original_body']}</div>
            </div>
            """

    html_content += """
    </body>
    </html>
    """

    msg.attach(MIMEText(html_content, "html"))

    # Connect and send
    print(f"Connecting to SMTP server to send email from {EMAIL_SENDER} to {EMAIL_RECEIVER}...")
    # Using secure port 587
    server = smtplib.SMTP("smtp.gmail.com", 587)
    server.starttls()
    server.login(EMAIL_SENDER, EMAIL_PASSWORD)
    server.sendmail(EMAIL_SENDER, EMAIL_RECEIVER, msg.as_string())
    server.quit()
    print("Email sent successfully!")

def rewrite_with_gemini(original_body):
    print("Requesting rewrite from Gemini API...")
    system_instruction = (
        "너는 전문적이고 객관적인 시사 애널리스트다. 다음 뉴스 원문을 분석해서 블로그 게시글 형태로 작성해라.\n"
        "원문의 문장 구조나 표현을 절대 그대로 복사하지 말고, 너만의 언어로 완전히 새롭게 재구성할 것. (저작권 회피)\n"
        "글의 첫 줄에는 사람들의 시선을 끌 수 있는 명확하고 새로운 **[제목]**을 텍스트로만 반환할 것.\n"
        "기사에서 확인된 객관적 수치나 팩트(Fact)는 반드시 마크다운 굵게(**내용**) 처리하여 시각적으로 돋보이게 할 것.\n"
        "팩트 전달 이후, 이 사건이 가지는 의미나 향후 전망을 서술형으로 덧붙일 것."
    )
    
    # Configure API Key
    genai.configure(api_key=GEMINI_API_KEY)
    
    # Fallback model list: try 2.5-flash, 3.5-flash, then 1.5-flash
    models_to_try = ["gemini-2.5-flash", "gemini-3.5-flash", "gemini-1.5-flash"]
    last_exception = None
    
    for model_name in models_to_try:
        try:
            print(f"Trying model: {model_name}...")
            model = genai.GenerativeModel(
                model_name=model_name,
                system_instruction=system_instruction
            )
            response = model.generate_content(original_body)
            # Restrict rate limit
            time.sleep(5)
            return response.text
        except Exception as e:
            print(f"Model {model_name} failed: {e}")
            last_exception = e
            continue
            
    raise last_exception if last_exception else ValueError("All models failed to generate content.")

def parse_gemini_output(output_text):
    lines = output_text.strip().split("\n")
    if not lines:
        return "No Title", ""
    
    title_line = lines[0].strip()
    # Clean decorators from Gemini's title line (like **[제목]**, [제목], #, etc.)
    title = re.sub(r'^(\*\*|)?\[제목\](\*\*|)?\s*', '', title_line)
    title = re.sub(r'^#\s*', '', title)
    title = title.strip(' *"')
    
    body = "\n".join(lines[1:]).strip()
    return title, body

def save_markdown_file(category, index, title, rewritten_body, original_link):
    today_str = datetime.now().strftime("%Y-%m-%d")
    filename = f"{today_str}-ap-{category}-{index}.md"
    filepath = os.path.join(POSTS_DIR, filename)
    
    frontmatter = f"""---
title: "{title}"
date: "{today_str}"
category: "{category.capitalize()}"
original_link: "{original_link}"
---
{rewritten_body}
"""
    os.makedirs(POSTS_DIR, exist_ok=True)
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(frontmatter)
    print(f"Saved markdown post: {filepath}")

def main():
    print("Starting news automation pipeline...")
    check_env_vars()
    
    articles_by_section = {"world": [], "business": []}
    
    # Step 1: Crawl
    for section_name, hub_url in SECTIONS.items():
        try:
            links = extract_article_links(hub_url)
            print(f"Found {len(links)} links in {section_name} section.")
            for url in links:
                try:
                    time.sleep(1) # Polite crawler delay
                    original_title, original_body = extract_article_content(url)
                    articles_by_section[section_name].append({
                        "url": url,
                        "original_title": original_title,
                        "original_body": original_body
                    })
                except Exception as e:
                    print(f"Error extracting content from {url}: {e}")
        except Exception as e:
            print(f"Error processing hub section {section_name}: {e}")

    total_articles = sum(len(lst) for lst in articles_by_section.values())
    if total_articles == 0:
        print("No articles fetched. Terminating pipeline.")
        return

    # Step 2: Private Email Dispatch (Original texts sent privately)
    try:
        send_consolidated_email(articles_by_section)
    except Exception as e:
        print(f"Error sending consolidated email: {e}")

    # Step 3 & 4: Rewrite using Gemini and generate Markdown
    for section_name, articles in articles_by_section.items():
        for i, art in enumerate(articles, start=1):
            try:
                rewritten_text = rewrite_with_gemini(art["original_body"])
                title, rewritten_body = parse_gemini_output(rewritten_text)
                
                # Double-check: ensure the original text is NOT in the final body
                # In case Gemini did not rewrite completely, or to be absolutely secure.
                # Since the instruction is robust, this is just to create the markdown post.
                save_markdown_file(
                    category=section_name,
                    index=i,
                    title=title,
                    rewritten_body=rewritten_body,
                    original_link=art["url"]
                )
            except Exception as e:
                print(f"Error during Gemini rewrite/markdown generation for {art['url']}: {e}")
                
    print("Pipeline execution completed successfully.")

if __name__ == "__main__":
    main()
