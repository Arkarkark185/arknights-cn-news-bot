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


def parse_sina_page(raw: str) -> list[Post]:
    """Sina publicly republishes the official account's Weibo posts.

    Use source-specific detail links so the same post is recognized every run.
    This is a fallback; links open the Sina copy instead of a direct Weibo post.
    """
    soup = BeautifulSoup(raw, 'html.parser')
    posts = []
    for anchor in soup.find_all('a', href=True):
        url = urljoin('https://sina.cn/', anchor['href'])
        match = re.fullmatch(r'https?://(?:www\.)?sina\.cn/news/detail/(\d+)\.html(?:\?.*)?', url)
        if not match:
            continue
        content = clean_text(anchor.get_text(' ', strip=True))
        # Remove the publisher/date/"posted from" prefix, not the news itself.
        content = re.sub(r'^明日方舟Arknights\s+20\d\d-\d\d-\d\d\s+\d\d:\d\d来自\s*[^#]*', '', content)
        content = content.strip()
        if len(content) < 8:
            continue
        first_img = anchor.find('img', src=True)
        picture = absolute_image(url, first_img['src']) if first_img else None
        posts.append(Post('Official Weibo (Sina mirror)', f'sina:{match.group(1)}', content, url, picture))
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
        posts.append(Post('Official Bilibili', f'bilibili:{id_str}', text, url,
                          absolute_image('https://www.bilibili.com/', image)))
    if not posts:
        raise ValueError('Bilibili response had no usable dynamics')
    return unique_posts(posts)


def parse_bilibili_rss(raw: str) -> list[Post]:
    """Read an RSSHub feed for the official Bilibili account.

    This is an independently hosted, best-effort fallback, not an official API.
    Accept links pointing to Bilibili only, and reject empty/error feeds.
    """
    root = ET.fromstring(raw)
    posts = []
    for item in root.findall('.//item'):
        title = clean_text(item.findtext('title') or '')
        description = item.findtext('description') or ''
        encoded = item.findtext('{http://purl.org/rss/1.0/modules/content/}encoded') or ''
        body = encoded or description
        text = clean_text(title + ' | ' + body)
        link = (item.findtext('link') or '').strip()
        parsed = urlparse(link)
        if parsed.scheme != 'https' or not (parsed.hostname or '').endswith(('.bilibili.com', '.b23.tv')):
            continue
        raw_id = (item.findtext('guid') or link).strip()
        if not raw_id or not text:
            continue
        image = None
        document = BeautifulSoup(body, 'html.parser')
        first = document.find('img')
        if first:
            image = absolute_image(link, first.get('src') or first.get('data-src'))
        # Links/IDs remain the same even if a different RSSHub instance is used.
        posts.append(Post('Official Bilibili (RSS mirror)', f'bili-rss:{raw_id}',
                          shorten(text, 1800), link, image))
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


def enrich_image(post: Post) -> str | None:
    if post.image:
        return post.image
    if post.source not in ('Official site', 'Official Weibo (Sina mirror)'):
        return None
    try:
        soup = BeautifulSoup(get_html(post.url), 'html.parser')
        meta = soup.find('meta', attrs={'property': 'og:image'}) or soup.find('meta', attrs={'name': 'twitter:image'})
        if meta and meta.get('content'):
            return absolute_image(post.url, meta['content'])
        # Only try images inside the actual article; don't attach navigation assets.
        article = soup.select_one('article, main, .news-detail, .news-content, .article, .article-content')
        if article:
            for img in article.find_all('img'):
                url = img.get('src') or img.get('data-src')
                if url and ('hycdn' in url or 'sinaimg' in url or '/news/' in url):
                    return absolute_image(post.url, url)
    except requests.RequestException as exc:
        LOG.warning('Could not load article image: %s', exc)
    return None


def translated(text: str) -> str | None:
    """Free, unofficial Google Translate web integration. May be rate-limited."""
    try:
        from deep_translator import GoogleTranslator
        return GoogleTranslator(source='auto', target='en').translate(text[:1900])
    except Exception as exc:
        LOG.warning('English translation failed (sending Chinese original): %s', exc)
        return None


def shorten(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit - 1].rstrip() + '…'


def send_to_discord(webhook: str, post: Post, *, dry_run: bool = False) -> None:
    english = translated(post.text) if not dry_run else None
    title = shorten((english or post.text).split(' | ')[0], 220)
    chinese = shorten(post.text, 690)
    translated_text = shorten(english, 690) if english else '*English translation unavailable this run.*'
    desc = f'**English (automatic translation)**\n{translated_text}\n\n**中文原文**\n{chinese}'
    embed = {
        'title': title,
        'url': post.url,
        'description': desc,
        'color': 0x36B5C5,
        'footer': {'text': post.source + ' • Arknights CN'},
    }
    image = enrich_image(post) if not dry_run else post.image
    if image:
        embed['image'] = {'url': image}
    payload = {
        'username': 'Arknights CN News',
        'allowed_mentions': {'parse': []},
        'embeds': [embed],
    }
    if dry_run:
        LOG.info('[DRY RUN] %s - %s', post.source, shorten(post.text, 100))
        return
    send_payload(webhook, payload)


def send_payload(webhook: str, payload: dict) -> None:
    if not webhook.startswith(('https://discord.com/api/webhooks/',
                               'https://discordapp.com/api/webhooks/')):
        raise ValueError('Webhook URL must be a Discord incoming-webhook URL')
    for attempt in range(3):
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
    send_payload(webhook, {
        'username': 'Arknights CN News',
        'allowed_mentions': {'parse': []},
        'embeds': [{
            'title': '✅ Arknights CN news bot connected!',
            'description': 'Discord notifications are working. Run the normal workflow '
                           'once to establish your starting point. New announcements '
                           'will be sent on later checks.',
            'color': 0x36B5C5,
            'url': SITE,
        }],
    })
    LOG.info('Sent a test message to Discord')


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
