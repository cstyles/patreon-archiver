from os import chdir, makedirs
from pathlib import Path
from typing import Iterator, Optional, TypedDict, Union
import json
import sys

from loguru import logger
from ratelimiter import RateLimiter
from yt_dlp.cookies import extract_cookies_from_browser
import click
import requests
import yt_dlp

from .constants import MEDIA_URI, POSTS_URI, SHARED_HEADERS
from .patreon_typing import PostDataDict, PostDataImageDict, PostsDict
from .utils import (YoutubeDLLogger, chunks, get_extension, get_shared_params,
                    setup_logging, unique_iter, write_if_new)

__all__ = ('main',)


class SaveInfo(TypedDict):
    post_data_dict: PostDataDict
    target_dir: Path


def save_images(session: requests.Session, pdd: PostDataDict,
                sleep_time: int) -> SaveInfo:
    click.secho(f"Image file: {pdd['attributes']['url']}")
    target_dir = Path('.', 'images', pdd['id'])
    makedirs(target_dir, exist_ok=True)
    write_if_new(target_dir.joinpath('post.json'),
                 f'{json.dumps(pdd, sort_keys=True, indent=2)}\n')
    rate_limiter = RateLimiter(max_calls=1, period=sleep_time)
    for index, id_ in enumerate(
            pdd['attributes']['post_metadata']['image_order'], start=1):
        with rate_limiter:
            with session.get(f'{MEDIA_URI}/{id_}') as r:
                data: PostDataImageDict = r.json()['data']
                with session.get(
                        data['attributes']['image_urls']['original']) as r:
                    write_if_new(
                        target_dir.joinpath(
                            f'{index:02d}-{data["id"]}.' +
                            get_extension(data["attributes"]["mimetype"])),
                        r.content, 'wb')
    return SaveInfo(post_data_dict=pdd, target_dir=target_dir)


def save_other(pdd: PostDataDict) -> SaveInfo:
    click.secho(f"{pdd['attributes']['post_type'].title()}: " +
                pdd['attributes']['url'])
    other = Path('.', 'other')
    makedirs(other, exist_ok=True)
    write_if_new(
        other.joinpath(f"{pdd['attributes']['post_type']}-{pdd['id']}.json"),
        f'{json.dumps(pdd, sort_keys=True, indent=2)}\n')
    return SaveInfo(post_data_dict=pdd, target_dir=other)


def process_posts(posts: PostsDict, session: requests.Session,
                  sleep_time: int) -> Iterator[Union[str, SaveInfo]]:
    for post in posts['data']:
        if (post['attributes']['post_type']
                in ('audio_file', 'audio_embed', 'video_embed')):
            yield post['attributes']['url']
        elif post['attributes']['post_type'] == 'image_file':
            yield from save_images(session, post, sleep_time)
        else:
            yield save_other(post)


@click.command()
@click.option('-o', '--output-dir', default=None, help='Output directory')
@click.option('-b',
              '--browser',
              default='chrome',
              help='Browser to read cookies from')
@click.option('-p', '--profile', default='Default', help='Browser profile')
@click.option('-x',
              '--fail',
              is_flag=True,
              help=('Do not continue processing after a failed '
                    'yt-dlp command.'))
@click.option('-L',
              '--yt-dlp-arg-limit',
              default=20,
              type=int,
              help='Number of media URIs to pass to yt-dlp at a time.')
@click.option('-S',
              '--sleep-time',
              default=1,
              type=int,
              help='Number of seconds to wait between requests')
@click.option('-d', '--debug', is_flag=True, help='Enable debug output')
@click.argument('campaign_id')
def main(output_dir: Optional[Union[Path, str]],
         browser: str,
         profile: str,
         campaign_id: str,
         fail: bool = False,
         yt_dlp_arg_limit: int = 20,
         sleep_time: int = 1,
         debug: bool = False) -> None:
    setup_logging(debug)
    if output_dir is None:
        output_dir = Path('.', campaign_id)
        makedirs(output_dir, exist_ok=True)
    chdir(output_dir)
    with requests.Session() as session:
        session.headers.update({
            **SHARED_HEADERS,
            **dict(cookie='; '.join(f'{c.name}={c.value}' \
                for c in extract_cookies_from_browser(browser, profile)
                    if 'patreon.com' in c.domain))
        })
        with session.get(POSTS_URI,
                         params=get_shared_params(campaign_id)) as r:
            r.raise_for_status()
            posts: PostsDict = r.json()
            media_uris = list(
                x for x in process_posts(posts, session, sleep_time)
                if isinstance(x, str))
            next_uri: Optional[str] = posts['links']['next']
            logger.debug(f'Next URI: {next_uri}')
            rate_limiter = RateLimiter(max_calls=1, period=sleep_time)
            while next_uri:
                with rate_limiter:
                    with session.get(next_uri) as r:
                        r.raise_for_status()
                        posts = r.json()
                        media_uris.extend(
                            x
                            for x in process_posts(posts, session, sleep_time)
                            if isinstance(x, str))
                        try:
                            next_uri = posts['links']['next']
                            logger.debug(f'Next URI: {next_uri}')
                        except KeyError:
                            next_uri = None
            sys.argv = [sys.argv[0]]
            ydl_opts = yt_dlp.parse_options()[-1]
            with yt_dlp.YoutubeDL({
                    **ydl_opts,
                    **dict(http_headers=SHARED_HEADERS,
                           logger=YoutubeDLLogger(),
                           sleep_interval_requests=sleep_time,
                           verbose=debug)
            }) as ydl:
                for chunk in chunks(list(unique_iter(media_uris)),
                                    yt_dlp_arg_limit):
                    try:
                        ydl.download(list(chunk))
                    except Exception as e:
                        if fail:
                            raise click.Abort() from e
