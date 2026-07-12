import os
import re
import time
import json
from datetime import datetime, timedelta
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

def rewrite_with_gemini(original_body, art_url):
    print("Requesting rewrite from Gemini API...")
    system_instruction = (
        "당신은 객관적인 팩트를 바탕으로 깊이 있는 인사이트를 도출하는 전문 애널리스트 겸 테크/경제 블로거입니다.\n"
        "당신의 목표는 제공된 뉴스 기사에서 '사실(Fact)'만을 추출한 뒤, 원본의 문장 구조나 표현을 절대 모방하지 않고 당신만의 독창적인 시각과 구조로 완전히 새로운 포스트를 작성하는 것입니다."
    )
    
    # Configure API Key
    genai.configure(api_key=GEMINI_API_KEY)
    
    # Fallback model list: try 2.5-flash, 3.5-flash, then 1.5-flash
    models_to_try = ["gemini-2.5-flash", "gemini-3.5-flash", "gemini-1.5-flash"]
    last_exception = None
    
    user_prompt = f"""아래 제공된 AP 뉴스 기사를 바탕으로 다음 규칙을 엄격히 준수하여 마크다운(.md) 형식의 블로그 포스트를 작성해 주세요.

[규칙]
1. 원본 회피 (Avoid Plagiarism): 원본 기사의 문장, 구문, 문단 구조를 그대로 번역하거나 복사하지 마세요. 사실 관계(숫자, 이름, 사건)만 추출하여 완전히 새로운 흐름으로 재구성하세요.
2. 가치 창출 (Provide Commentary): 단순한 사건 요약으로 끝내지 마세요. 이 사건이 왜 중요한지, 향후 어떤 영향을 미칠지에 대한 '분석'이나 '시사점(Key Takeaway)' 섹션을 반드시 포함하여 새로운 가치를 더하세요.
3. 양식 준수 (Format & Cite): 결과물은 반드시 아래 마크다운 구조를 따르며, 마지막에 원본 출처와 면책조항을 명시해야 합니다.

[출력 마크다운 구조]
# [흥미롭고 새로운 제목을 생성하세요]

## 📌 핵심 요약
(3~4개의 글머리 기호로 핵심 팩트를 요약)

## 📖 주요 내용
(새로운 구조와 자신의 언어로 사건의 전말을 서술)

## 💡 시사점 및 분석
(이 뉴스가 가지는 의미, 업계나 사회에 미칠 영향 등 논평 추가)

---
**Source:** [AP News 원본 기사 읽기]({art_url})
**Disclaimer:** While referencing AP News reports for factual background, the core of this post is the author's independent analysis and subjective insights.

[뉴스 원본 텍스트]: 
{original_body}"""

    for model_name in models_to_try:
        try:
            print(f"Trying model: {model_name}...")
            model = genai.GenerativeModel(
                model_name=model_name,
                system_instruction=system_instruction
            )
            response = model.generate_content(user_prompt)
            # Restrict rate limit
            time.sleep(5)
            return response.text
        except Exception as e:
            print(f"Model {model_name} failed: {e}")
            last_exception = e
            continue
            
    raise last_exception if last_exception else ValueError("All models failed to generate content.")

def parse_gemini_output(output_text):
    lines = [line.strip() for line in output_text.strip().split("\n")]
    # Find the first non-empty line
    title_index = -1
    for idx, line in enumerate(lines):
        if line:
            title_index = idx
            break
            
    if title_index == -1:
        return "No Title", ""
        
    title_line = lines[title_index]
    # Clean decorators from Gemini's title line (like **[제목]**, [제목], #, etc.)
    title = re.sub(r'^#\s*', '', title_line)
    title = re.sub(r'^(\*\*|)?\[제목\](\*\*|)?\s*', '', title)
    title = title.strip(' *"[]')
    
    # Body starts from the next non-empty lines
    body_lines = lines[title_index + 1:]
    body = "\n".join(body_lines).strip()
    return title, body

def extract_summary(rewritten_body):
    content = rewritten_body.replace('\r\n', '\n')
    summary_header = "## 📌 핵심 요약"
    main_header = "## 📖 주요 내용"
    
    start_idx = content.find(summary_header)
    if start_idx != -1:
        start_idx += len(summary_header)
        end_idx = content.find(main_header, start_idx)
        if end_idx != -1:
            summary = content[start_idx:end_idx].strip()
        else:
            summary = content[start_idx:].strip()
    else:
        summary = ""
    
    summary = re.sub(r'^---|---$', '', summary).strip()
    return summary

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
    # We do NOT clear the _posts folder anymore (Strategy A: Never Delete)
    
    today_str = datetime.now().strftime("%Y-%m-%d")
    new_articles_metadata = []

    for section_name, articles in articles_by_section.items():
        for i, art in enumerate(articles, start=1):
            try:
                rewritten_text = rewrite_with_gemini(art["original_body"], art["url"])
                title, rewritten_body = parse_gemini_output(rewritten_text)
                
                save_markdown_file(
                    category=section_name,
                    index=i,
                    title=title,
                    rewritten_body=rewritten_body,
                    original_link=art["url"]
                )
                
                # Extract summary and gather metadata for index
                summary_md = extract_summary(rewritten_body)
                new_articles_metadata.append({
                    "slug": f"{today_str}-ap-{section_name}-{i}",
                    "title": title,
                    "date": today_str,
                    "category": section_name.capitalize(),
                    "originalLink": art["url"],
                    "summaryMd": summary_md
                })
            except Exception as e:
                print(f"Error during Gemini rewrite/markdown generation for {art['url']}: {e}")

    # Step 5: Incremental index.json updates (O(1) complexity)
    index_path = os.path.join(POSTS_DIR, "index.json")
    existing_articles = []
    
    if os.path.exists(index_path):
        try:
            with open(index_path, "r", encoding="utf-8") as f:
                existing_articles = json.load(f)
            print(f"Loaded {len(existing_articles)} existing entries from index.json.")
        except Exception as e:
            print(f"Error loading index.json: {e}")
            
    # Fallback to date-search if index.json is empty/missing
    if not existing_articles:
        print("index.json not found or empty. Running O(1) date-search fallback...")
        for offset in range(1, 6):  # Check last 5 days
            check_date = (datetime.now() - timedelta(days=offset)).strftime("%Y-%m-%d")
            day_files = []
            for category in ["world", "business"]:
                for index in range(1, 10):
                    filename = f"{check_date}-ap-{category}-{index}.md"
                    filepath = os.path.join(POSTS_DIR, filename)
                    if os.path.exists(filepath):
                        day_files.append((category, index, filepath, check_date))
            
            for cat, idx, path_to_file, date_str in day_files:
                try:
                    with open(path_to_file, "r", encoding="utf-8") as f:
                        raw = f.read()
                    parts = raw.split("---")
                    if len(parts) >= 3:
                        fm_raw = parts[1]
                        body_raw = "---".join(parts[2:])
                        
                        title_match = re.search(r'title:\s*"(.*?)"', fm_raw)
                        link_match = re.search(r'original_link:\s*"(.*?)"', fm_raw)
                        
                        title = title_match.group(1) if title_match else "No Title"
                        link = link_match.group(1) if link_match else ""
                        
                        existing_articles.append({
                            "slug": f"{date_str}-ap-{cat}-{idx}",
                            "title": title,
                            "date": date_str,
                            "category": cat.capitalize(),
                            "originalLink": link,
                            "summaryMd": extract_summary(body_raw)
                        })
                except Exception as e:
                    print(f"Error parsing fallback file {path_to_file}: {e}")
        
        # Sort fallback articles descending
        existing_articles.sort(key=lambda x: (x["date"], x["slug"]), reverse=True)

    # Prepend new articles to existing, avoiding duplicates
    new_articles_metadata.sort(key=lambda x: x["slug"], reverse=True)
    seen_slugs = set(x["slug"] for x in new_articles_metadata)
    combined_articles = new_articles_metadata.copy()
    
    for art in existing_articles:
        if art["slug"] not in seen_slugs:
            combined_articles.append(art)
            seen_slugs.add(art["slug"])
            
    # Keep only the top 12 latest articles (typically Today's 6 + Yesterday's 6)
    combined_articles = combined_articles[:12]
    
    # Save back to index.json
    try:
        with open(index_path, "w", encoding="utf-8") as f:
            json.dump(combined_articles, f, ensure_ascii=False, indent=2)
        print(f"Successfully saved {len(combined_articles)} entries to index.json.")
    except Exception as e:
        print(f"Error saving updated index.json: {e}")
        
    print("Pipeline execution completed successfully.")

if __name__ == "__main__":
    main()
