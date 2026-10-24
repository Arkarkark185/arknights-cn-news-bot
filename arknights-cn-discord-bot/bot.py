"""Arknights CN news -> Discord. Public sources, best-effort adapters, no paid API."""
from __future__ import annotations

import argparse
import html
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlparse
import xml.etree.ElementTree as ET

import requests
from bs4 import BeautifulSoup

LOG = logging.getLogger('arknights')
ROOT = Path(__file__).resolve().parent
STATE_PATH = ROOT / 'state.json'
SITE = 'https://ak.hypergryph.com/'
WEIBO_UID = '6279793937'
BILIBILI_UID = '161775300'
MAX_NOTIFICATIONS_PER_SOURCE = 12
MAX_STORED_IDS = 300

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                  'AppleWebKit/537.36 (KHTML, like Gecko) '
                  'Chrome/130.0.0.0 Safari/537.36',
    'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.7',
}
SESSION = requests.Session()
SESSION.headers.update(HEADERS)


@dataclass(frozen=True)
class Post:
    source: str
    id: str
    text: str
    url: str
    image: str | None = None
    images: tuple[str, ...] = ()


def clean_text(value: str) -> str:
    text = BeautifulSoup(html.unescape(value or ''), 'html.parser').get_text(' ', strip=True)
    return re.sub(r'\s+', ' ', text).strip()


def absolute_image(base: str, candidate: str | None) -> str | None:
    if not candidate:
        return None
    url = urljoin(base, candidate)
    if urlparse(url).scheme not in ('https', 'http'):
        return None
    return url.replace('http://', 'https://', 1) if url.startswith('http://') else url


def get_html(url: str, *, referer: str | None = None) -> str:
    headers = {'Referer': referer} if referer else {}
    response = SESSION.get(url, headers=headers, timeout=(10, 20))
    response.raise_for_status()
    return response.text


def get_json(url: str, *, params: dict | None = None, referer: str | None = None) -> dict:
    headers = {'Referer': referer} if referer else {}
    response = SESSION.get(url, params=params, headers=headers, timeout=(10, 20))
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise ValueError('Unexpected JSON response')
    return data


def parse_official_page(raw: str) -> list[Post]:
    """The official CN landing page currently links its three newest /news/ID posts."""
    soup = BeautifulSoup(raw, 'html.parser')
    posts: list[Post] = []
    seen = set()
    for anchor in soup.find_all('a', href=True):
        url = urljoin(SITE, anchor['href'])
        if not re.fullmatch(r'https://ak\.hypergryph\.com/news/\d+', url):
            continue
        if url in seen:
            continue
        title = clean_text(anchor.get_text(' ', strip=True))
        title = re.sub(r'^(?:新闻|公告|活动)\s*\d{4}\s*//\s*\d{1,2}\s*/\s*\d{1,2}', '', title).strip()
        title = re.sub(r'^\d{4}\s*//\s*\d{1,2}\s*/\s*\d{1,2}', '', title).strip()
        if not title or title.upper() in ('READ MORE', '更多情报', 'GO BACK'):
            continue
        seen.add(url)
        posts.append(Post('Official site', f'site:{url.rsplit("/", 1)[-1]}', title, url))
    if not posts:
        raise ValueError('Official site HTML had no /news/ID links; page may have changed')
    return posts


def fetch_official() -> list[Post]:
    return parse_official_page(get_html(SITE))


def parse_weibo_api(data: dict) -> list[Post]:
    if data.get('ok') != 1:
        raise ValueError(f'Weibo API rejected the request: {data.get("msg", "unknown error")}')
    posts = []
    for card in data.get('data', {}).get('cards', []):
        candidates = card.get('card_group', []) if card.get('card_group') else [card]
        for candidate in candidates:
            blog = candidate.get('mblog') or {}
            blog_id = str(blog.get('id') or '')
            if not blog_id:
                continue
            text = clean_text(blog.get('text') or blog.get('raw_text') or '')
            if not text:
                continue
            pictures = blog.get('pics') or []
            picture = (pictures[0].get('large') or {}).get('url') or pictures[0].get('url') if pictures else None
            posts.append(Post('Official Weibo', f'weibo:{blog_id}', text,
                              f'https://m.weibo.cn/detail/{blog_id}',
                              absolute_image('https://m.weibo.cn/', picture)))
    if not posts:
        raise ValueError('Weibo API returned no usable posts')
    return unique_posts(posts)


def remove_sina_metadata(text: str) -> str:
    """Remove Sina's repost header and engagement numbers, not post content."""
    text = re.sub(r'^\s*明日方舟\s*Arknights\s*20\d{2}-\d{2}-\d{2}\s*\d{2}:\d{2}\s*来自\s*', '', text, flags=re.I)
    # After the date, Sina often inserts the publishing client before the actual post.
    text = re.sub(r'^(?:微博直播平台|微博网页(?:版)?|微博视频号|微博客户端|微博|iPhone客户端|Android客户端|超话)[\s:：]*', '', text)
    text = re.sub(r'(?:(?:\s*[·•]\s*|\s+)\d{1,7}){3}\s*$', '', text)
    return text.strip(' |\n')


def extract_image_urls(fragment: str, base: str, *, max_count: int = 4) -> tuple[str, ...]:
    """Pick actual post media, ignoring profile pictures and small page furniture."""
    soup = BeautifulSoup(fragment or '', 'html.parser')
    urls = []
    for tag in soup.select('img, meta[property="og:image"], meta[name="twitter:image"]'):
        candidates = [tag.get(k) for k in ('data-original', 'data-src', 'data-lazy-src', 'src', 'content')]
        if tag.get('srcset'):
            candidates.append(tag['srcset'].split(',')[0].strip().split(' ')[0])
        for candidate in candidates:
            url = absolute_image(base, candidate)
            if not url:
                continue
            bad = ('/avatar/', '/face/', 'portrait', '/emoji/', 'favicon', 'spacer', 'placeholder', 'default_avatar', '/logo/', 'tvax', 'tva1.sinaimg')
            if any(part in url.lower() for part in bad) or len(url) > 1900:
                continue
            if url not in urls:
                urls.append(url)
            break
        if len(urls) >= max_count:
            break
    return tuple(urls)


def parse_sina_page(raw: str) -> list[Post]:
    """Get news items from Sina's public mirror with possible post thumbnails."""
    soup = BeautifulSoup(raw, 'html.parser')
    posts = []
    for anchor in soup.find_all('a', href=True):
        url = urljoin('https://sina.cn/', anchor['href'])
        match = re.fullmatch(r'https?://(?:www\.)?sina\.cn/news/detail/(\d+)\.html(?:\?.*)?', url)
        if not match:
            continue
        content = remove_sina_metadata(clean_text(anchor.get_text(' ', strip=True)))
        if len(content) < 8:
            continue
        imgs = extract_image_urls(str(anchor), url)
        posts.append(Post('Official Weibo (Sina mirror)', f'sina:{match.group(1)}', content,
                          url, imgs[0] if imgs else None, imgs))
    if not posts:
        raise ValueError('Sina mirror had no matching /news/detail/ links')
    return unique_posts(posts)


def fetch_weibo() -> list[Post]:
    # Mobile Web API is unofficial and might be blocked from cloud IPs.
    try:
        data = get_json('https://m.weibo.cn/api/container/getIndex',
                        params={'containerid': f'230413{WEIBO_UID}', 'page': 1},
                        referer=f'https://m.weibo.cn/u/{WEIBO_UID}')
        return parse_weibo_api(data)
    except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
        LOG.warning('Weibo mobile API unavailable (%s); trying Sina mirror', exc)
    return parse_sina_page(get_html(f'https://sina.cn/media/{WEIBO_UID}'))


def parse_bilibili_api(data: dict) -> list[Post]:
    if data.get('code') != 0:
        raise ValueError(f'Bilibili API rejected the request: {data.get("code")} {data.get("message")}')
    posts: list[Post] = []
    for item in data.get('data', {}).get('items', []):
        id_str = str(item.get('id_str') or '')
        if not id_str:
            continue
        dynamic = (item.get('modules') or {}).get('module_dynamic') or {}
        major = dynamic.get('major') or {}
        desc = dynamic.get('desc') or {}
        archive = major.get('archive') or {}
        opus = major.get('opus') or {}
        draw = major.get('draw') or {}
        text = clean_text(desc.get('text') or (opus.get('summary') or {}).get('text') or
                          opus.get('title') or archive.get('title') or '')
        if archive.get('title') and archive.get('title') not in text:
            text = clean_text(archive['title'] + ' | ' + text)
        if not text:
            continue
        pictures = draw.get('items') or (opus.get('pics') or [])
        first_picture = pictures[0] if pictures else {}
        image = (archive.get('cover') or first_picture.get('src') or first_picture.get('url') or
                 (opus.get('cover') or {}).get('url'))
        bvid = archive.get('bvid')
        url = f'https://www.bilibili.com/video/{bvid}' if bvid else f'https://www.bilibili.com/opus/{id_str}'
        image_list = [absolute_image('https://www.bilibili.com/', image)] if image else []
        for pic in pictures[:4]:
            candidate = absolute_image('https://www.bilibili.com/', pic.get('src') or pic.get('url'))
            if candidate and candidate not in image_list:
                image_list.append(candidate)
        posts.append(Post('Official Bilibili', f'bilibili:{id_str}', text, url,
                          image_list[0] if image_list else None, tuple(image_list[:4])))
    if not posts:
        raise ValueError('Bilibili response had no usable dynamics')
    return unique_posts(posts)


def parse_bilibili_rss(raw: str) -> list[Post]:
    """Parse official-account items syndicated by a third-party RSSHub instance."""
    root = ET.fromstring(raw)
    posts = []
    for item in root.findall('.//item'):
        title = clean_text(item.findtext('title') or '')
        description = item.findtext('description') or ''
        encoded = item.findtext('{http://purl.org/rss/1.0/modules/content/}encoded') or ''
        body = encoded or description
        body_text = clean_text(body)
        # RSSHub often duplicates the entire title as the beginning of the body.
        if title and body_text.startswith(title):
            body_text = body_text[len(title):].lstrip(' |:：-')
        text = (title + (' — ' + body_text if body_text else '')) if title else body_text
        link = (item.findtext('link') or '').strip()
        parsed = urlparse(link)
        hostname = parsed.hostname or ''
        if parsed.scheme != 'https' or not (hostname == 'b23.tv' or hostname == 'bilibili.com' or hostname.endswith('.bilibili.com')):
            continue
        raw_id = (item.findtext('guid') or link).strip()
        if not raw_id or not text:
            continue
        imgs = list(extract_image_urls(body, link))
        for element in item.iter():
            if element.tag.split('}')[-1] not in ('enclosure', 'thumbnail', 'content'):
                continue
            url = absolute_image(link, element.attrib.get('url'))
            mime = element.attrib.get('type', '')
            if url and (mime.startswith('image/') or element.tag.split('}')[-1] == 'thumbnail' or re.search(r'\.(?:jpe?g|png|webp|gif)(?:[?@]|$)', url, re.I)) and url not in imgs:
                imgs.append(url)
        posts.append(Post('Official Bilibili (RSS mirror)', f'bili-rss:{raw_id}',
                          shorten(text, 1700), link, imgs[0] if imgs else None, tuple(imgs[:4])))
    if not posts:
        raise ValueError('RSS mirror returned no usable Bilibili posts')
    return unique_posts(posts)


def fetch_bilibili() -> list[Post]:
    try:
        data = get_json('https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space',
                        params={'host_mid': BILIBILI_UID},
                        referer=f'https://space.bilibili.com/{BILIBILI_UID}/dynamic')
        return parse_bilibili_api(data)
    except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
        LOG.warning('Bilibili direct API unavailable (%s); trying public RSS mirrors', exc)

    failures = []
    for instance in ('https://rsshub.app', 'https://rss.materium.io'):
        feed = f'{instance}/bilibili/user/dynamic/{BILIBILI_UID}'
        try:
            return parse_bilibili_rss(get_html(feed))
        except (requests.RequestException, ValueError, ET.ParseError) as exc:
            failures.append(f'{instance}: {exc}')
            LOG.warning('Bilibili RSS mirror unavailable: %s', failures[-1])
    raise RuntimeError('Bilibili API and RSS fallback unavailable; ' + '; '.join(failures))


def unique_posts(posts: list[Post]) -> list[Post]:
    ids = set()
    result = []
    for post in posts:
        if post.id not in ids:
            ids.add(post.id)
            result.append(post)
    return result


def source_images(post: Post, *, max_count: int = 3) -> list[str]:
    """Collect image URLs only for newly sent posts (not every old monitored post)."""
    result = []
    for url in ([post.image] if post.image else []) + list(post.images):
        if url and url not in result:
            result.append(url)
    if post.source in ('Official site', 'Official Weibo (Sina mirror)'):
        try:
            raw = get_html(post.url)
            soup = BeautifulSoup(raw, 'html.parser')
            # Prefer in-article images over preview icons and page-wide banners.
            article = soup.select_one('article, main, .news-detail, .news-content, .article, .article-content, .content')
            if article:
                for url in extract_image_urls(str(article), post.url):
                    if url not in result:
                        result.append(url)
            if not result:
                for meta in soup.select('meta[property="og:image"], meta[name="twitter:image"]'):
                    for url in extract_image_urls(str(meta), post.url):
                        if url not in result:
                            result.append(url)
        except requests.RequestException as exc:
            LOG.warning('Could not find article images: %s', exc)
    return result[:max_count]


def enrich_image(post: Post) -> str | None:
    """Backward-compatible helper for existing local scripts/tests."""
    imgs = source_images(post, max_count=1)
    return imgs[0] if imgs else None


def shorten(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit - 1].rstrip() + '…'


def translated(text: str) -> str | None:
    """Try free machine-translation services, with no subscription or API key."""
    # Translate excerpts, not copied link lists. This keeps within free limits.
    message = shorten(re.sub(r'https?://\S+', '', text).strip(), 900)
    if not message:
        return None
    if not re.search(r'[\u3400-\u9fff]', message):
        return message
    errors = []
    try:
        response = SESSION.get('https://translate.googleapis.com/translate_a/single',
                               params={'client': 'gtx', 'sl': 'zh-CN', 'tl': 'en', 'dt': 't', 'q': message},
                               timeout=(6, 12))
        response.raise_for_status()
        result = response.json()
        english = ''.join(str(part[0]) for part in result[0] if part and part[0]).strip()
        if english and english != message:
            return english
        raise ValueError('Empty translation')
    except (requests.RequestException, ValueError, IndexError, TypeError, KeyError) as exc:
        errors.append('Google endpoint: ' + str(exc))
    try:
        from deep_translator import GoogleTranslator
        result = GoogleTranslator(source='zh-CN', target='en').translate(message)
        if result and result != message:
            return str(result)
        raise ValueError('Empty translation')
    except Exception as exc:
        errors.append('Google HTML: ' + str(exc))
    # MyMemory's free anonymous API has a 500-byte request cap and a daily quota.
    # Keep this short enough for Chinese text and avoid paid keys or accounts.
    try:
        chunk = shorten(message, 130)
        while len(chunk.encode('utf-8')) > 450:
            chunk = chunk[:-1]
        response = SESSION.get('https://api.mymemory.translated.net/get',
                               params={'q': chunk, 'langpair': 'zh-CN|en'}, timeout=(6, 12))
        response.raise_for_status()
        data = response.json()
        if int(data.get('responseStatus', 0)) != 200:
            raise ValueError('Translation provider returned non-200 status')
        result = html.unescape(str(data.get('responseData', {}).get('translatedText') or ''))
        if result and result != chunk and not result.startswith('MYMEMORY WARNING'):
            return result + (' …' if len(chunk) < len(message) else '')
        raise ValueError('Empty translation')
    except (requests.RequestException, ValueError, TypeError, KeyError) as exc:
        errors.append('MyMemory: ' + str(exc))
    LOG.warning('English translation unavailable: %s', ' | '.join(errors))
    return None


def category(text: str) -> tuple[str, int]:
    """Heuristic label, not a claim about the content's official category."""
    rules = (
        (r'模组|module', 'Module update', 0x8C77DD),
        (r'时装|皮肤|服饰|skin|outfit', 'New outfit / skin', 0xF498C2),
        (r'干员|角色|技能|operator', 'Operator news', 0xE5B65C),
        (r'维护|停机|闪断|服务器|更新公告|maintenance', 'Maintenance notice', 0xE78B55),
        (r'直播|特别节目|前瞻|PV|预告片|livestream', 'Livestream / trailer', 0xAE82D6),
        (r'活动|限时|复刻|主题曲|event', 'Event announcement', 0x46AFC4),
    )
    for pattern, label, color in rules:
        if re.search(pattern, text, re.I):
            return label, color
    return 'Official announcement', 0x36B5C5


def tidy_post_text(text: str) -> str:
    text = remove_sina_metadata(text)
    text = re.sub(r'https?://\S+', '', text)
    text = re.sub(r'#明日方舟\[超话\]#|#明日方舟超话#|#明日方舟#', '', text)
    return re.sub(r'\s+', ' ', text).strip(' |—–-')


def build_embed(post: Post, english: str | None, *, preview: bool = False) -> dict:
    category_name, color = category(post.text)
    text = tidy_post_text(post.text)
    # The English text belongs front and center; the Chinese is retained in a compact spoiler.
    if english:
        english_clean = tidy_post_text(english)
        first_line = english_clean.split(' — ')[0].split(' | ')[0]
        first_sentence = re.split(r'(?<=[.!?])\s+', first_line, maxsplit=1)[0]
        title = shorten(first_sentence, 92)
        if len(title) < 5:
            title = category_name
        body = shorten(english_clean, 1250)
    else:
        title = category_name
        body = 'Free automatic translation is temporarily unavailable. See the Chinese original below.'
    title = ('🧪 Preview • ' if preview else '') + title
    embed = {
        'author': {'name': f'Arknights CN  •  {category_name}'},
        'title': shorten(title, 220),
        'url': post.url,
        'description': '**English translation**\n' + body + '\n\n[🔗 Open original announcement](' + post.url + ')',
        'color': color,
        'fields': [
            {'name': '中文原文  •  Click to reveal',
             'value': '||' + shorten(text or post.text, 800).replace('||', ' ') + '||', 'inline': False},
        ],
        'footer': {'text': post.source + '  •  Automatic translation may contain errors'},
    }
    return embed


def try_download_image(url: str, *, referer: str) -> tuple[bytes, str] | None:
    """Attach actual image bytes so Discord isn't dependent on image hotlink permission."""
    try:
        host = urlparse(url).hostname or ''
        if urlparse(url).scheme not in ('https', 'http') or not host:
            return None
        if host in ('localhost', '127.0.0.1') or host.startswith(('127.', '10.', '192.168.', '169.254.')):
            return None
        with SESSION.get(url, headers={'Referer': referer}, timeout=(7, 18), stream=True) as response:
            response.raise_for_status()
            kind = response.headers.get('Content-Type', '').split(';')[0].lower()
            ext = {'image/jpeg': 'jpg', 'image/png': 'png', 'image/webp': 'webp', 'image/gif': 'gif'}.get(kind)
            if not ext:
                return None
            output = bytearray()
            for block in response.iter_content(65536):
                output.extend(block)
                if len(output) > 4_500_000:
                    return None
            if not output:
                return None
            return bytes(output), ext
    except requests.RequestException as exc:
        LOG.info('Image download unavailable; trying image link instead: %s', exc)
        return None


def send_to_discord(webhook: str, post: Post, *, dry_run: bool = False, preview: bool = False) -> None:
    english = translated(tidy_post_text(post.text)) if not dry_run else None
    embed = build_embed(post, english, preview=preview)
    payload = {'username': 'Arknights CN News', 'allowed_mentions': {'parse': []}, 'embeds': [embed]}
    if dry_run:
        LOG.info('[DRY RUN] %s - %s', post.source, shorten(post.text, 100))
        return
    images = source_images(post)
    files = []
    for url in images:
        if len(files) >= 3:
            break
        img = try_download_image(url, referer=post.url)
        if img:
            data, ext = img
            if sum(len(f[1]) for f in files) + len(data) > 9_000_000:
                continue
            filename = f'arknights_{len(files) + 1}.{ext}'
            files.append((filename, data, 'image/jpeg' if ext == 'jpg' else f'image/{ext}'))
    if files:
        embed['image'] = {'url': 'attachment://' + files[0][0]}
        for filename, _, _ in files[1:]:
            payload['embeds'].append({'image': {'url': 'attachment://' + filename}, 'color': embed['color']})
    elif images:
        # Some hosts allow Discord to fetch the image directly, even when our download fails.
        embed['image'] = {'url': images[0]}
    else:
        LOG.info('No suitable attached image on %s; text-only post', post.source)
    send_payload(webhook, payload, files=files)


def send_payload(webhook: str, payload: dict, *, files: list[tuple[str, bytes, str]] | None = None) -> None:
    if not webhook.startswith(('https://discord.com/api/webhooks/',
                               'https://discordapp.com/api/webhooks/')):
        raise ValueError('Webhook URL must be a Discord incoming-webhook URL')
    for attempt in range(3):
        if files:
            upload = {f'files[{i}]': (filename, data, mime) for i, (filename, data, mime) in enumerate(files)}
            response = SESSION.post(webhook,
                                    data={'payload_json': json.dumps(payload, ensure_ascii=False)},
                                    files=upload, timeout=(10, 40))
        else:
            response = SESSION.post(webhook, json=payload, timeout=(10, 20))
        if response.status_code in (200, 204):
            return
        if response.status_code == 429 and attempt < 2:
            try:
                delay = min(float(response.json().get('retry_after', 2)), 20)
            except (ValueError, TypeError):
                delay = 2
            LOG.warning('Discord rate limit, retrying after %s seconds', delay)
            time.sleep(delay + 0.25)
            continue
        raise RuntimeError(f'Discord rejected webhook: HTTP {response.status_code}, '
                           f'{response.text[:120]}')


def send_test_message(webhook: str) -> None:
    """Show a real, formatted news preview without changing saved post IDs."""
    try:
        latest = fetch_official()
        send_to_discord(webhook, latest[0], preview=True)
        LOG.info('Sent latest official article as a preview; state.json unchanged')
        return
    except Exception as exc:
        LOG.warning('Could not generate live preview (%s); sending connection test', exc)
    send_payload(webhook, {
        'username': 'Arknights CN News',
        'allowed_mentions': {'parse': []},
        'embeds': [{
            'title': '✅ Arknights CN news bot connected!',
            'description': 'Discord is working. Live article preview was unavailable; '
                           'normal news monitoring remains enabled.',
            'color': 0x36B5C5,
            'url': SITE,
        }],
    })
    LOG.info('Sent a Discord connection test')


def load_state(path: Path) -> dict:
    if not path.exists():
        return {'sources': {}}
    data = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(data, dict) or not isinstance(data.get('sources'), dict):
        raise ValueError('Invalid state.json, expected {"sources": {}}')
    return data


def save_state(path: Path, state: dict) -> None:
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    tmp.replace(path)


def process_source(source: str, posts: list[Post], state: dict, webhook: str,
                   dry_run: bool = False) -> tuple[int, int]:
    if not posts:
        raise ValueError('Empty news list; refusing to mark source initialized')
    posts = unique_posts(posts)
    saved = state.setdefault('sources', {})
    ids = [p.id for p in posts]
    if source not in saved:
        saved[source] = ids[:MAX_STORED_IDS]
        LOG.info('%s: first successful read; saved %s existing posts without notifying', source, len(ids))
        return (0, 0)

    previous = saved[source]
    prev_set = set(previous)
    fresh = [post for post in reversed(posts) if post.id not in prev_set]
    if len(fresh) > MAX_NOTIFICATIONS_PER_SOURCE:
        LOG.warning('%s: %s unseen posts, sending newest %s; older posts skipped',
                    source, len(fresh), MAX_NOTIFICATIONS_PER_SOURCE)
        fresh = fresh[-MAX_NOTIFICATIONS_PER_SOURCE:]
    sent = 0
    for post in fresh:
        # On webhook failure don't count the post as seen. The job will retry next run.
        send_to_discord(webhook, post, dry_run=dry_run)
        if not dry_run:
            sent += 1
            # Preserve progress even if a later Discord delivery fails.
            previous = [post.id] + previous
            saved[source] = previous[:MAX_STORED_IDS]
        LOG.info('%s: new post %s', source, post.url)
    # After success also mark older skipped entries, if any, as processed.
    if not dry_run:
        saved[source] = list(dict.fromkeys(ids + previous))[:MAX_STORED_IDS]
    return (sent, len(fresh))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Free official Arknights CN news monitor for Discord')
    parser.add_argument('--test', action='store_true', help='Send just one test Discord message')
    parser.add_argument('--dry-run', action='store_true', help='Check feeds without sending messages')
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format='%(levelname)s | %(message)s')
    webhook = os.environ.get('DISCORD_WEBHOOK_URL', '').strip()
    if args.test:
        if not webhook:
            LOG.error('Set DISCORD_WEBHOOK_URL before testing Discord')
            return 2
        send_test_message(webhook)
        return 0
    if not webhook and not args.dry_run:
        LOG.error('Set DISCORD_WEBHOOK_URL in GitHub Actions secrets or your local environment')
        return 2
    state = load_state(STATE_PATH)
    succeeded = 0
    failed = 0
    for name, fetcher in [('official', fetch_official), ('weibo', fetch_weibo),
                          ('bilibili', fetch_bilibili)]:
        try:
            posts = fetcher()
            # Direct API and fallback mirror use different IDs; seed each independently
            # so switching providers never creates a flood of stale notifications.
            key = ('weibo-sina' if posts[0].id.startswith('sina:') else 'weibo-api') if name == 'weibo' else name
            if name == 'bilibili' and posts[0].id.startswith('bili-rss:'):
                key = 'bilibili-rss'
            notifications, new_count = process_source(key, posts, state, webhook, args.dry_run)
            LOG.info('%s: inspected %s posts, %s new, %s sent',
                     key, len(posts), new_count, notifications)
            succeeded += 1
        except Exception as exc:
            # Keep the other feeds working. A blocked API is not "no news".
            LOG.error('%s: could not check this feed: %s', name, exc)
            failed += 1
    # Commit only on real runs. --dry-run should not create local baseline state.
    if not args.dry_run:
        save_state(STATE_PATH, state)
    LOG.info('Finished: %s source(s) checked, %s failed', succeeded, failed)
    return 1 if succeeded == 0 else 0


if __name__ == '__main__':
    sys.exit(main())
