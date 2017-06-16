#!/usr/bin/env python
# -*- coding: utf-8 -*-

import StringIO
import argparse
import glob
import os
import re
import time
import zipfile

import Levenshtein
import dryscrape
import progressbar
import requests
import transmissionrpc
from bs4 import BeautifulSoup
from enum import Enum
from fake_useragent import UserAgent

SEEKER_THRESHOLD = 50
LEECHES_THRESHOLD = 5
MAX_NUMBER_OF_EPISODE_PER_SEASON = 20

MAGNET_REGEX = re.compile("^magnet")
SERIES_SIZE_REGEX = re.compile("size (\d+).*mib")
SUBSCENTER_DOWNLOAD_REGEX = re.compile("\?(.*?)'")
OPENSUBTITLES_DOWNLOAD_REGEX = re.compile("subtitles/(\d+)/")
OPENSUBTITLES_REFERRER_REGEX = re.compile("\'(.+)\'")
AUTHORITY_REGEX = re.compile("https://(.+?)/")
VERSION_REGEX_PATTERN = "{series}.*{episode_details}\.(.+?)(?:\.mkv)?(?:download at|$)"

CANONIZE_LANG = {"en": "eng", "he": "heb"}
HEBREW = "he"
ENGLISH = "en"

OPENSUBTITLES_BASE_URL = "https://www.opensubtitles.org"

Status = Enum('Status', 'success no_connection no_results no_good_results generic_error')


class BaseDownloader(object):
    def __init__(self):
        self.user_agent = UserAgent()
        self.session = dryscrape.Session()

    def set_crawling_attributes(self):
        print "Setting %s crawler attributes" % self.__class__.__name__
        self.session.set_header('User-Agent', self.user_agent.random)

    @staticmethod
    def get_episode_name(series, season_number, episode_number):
        return "{series}.s{season}e{episode}".format(series=series.replace(' ', '.'), season=season_number,
                                                     episode=episode_number)

    @staticmethod
    def extract_details_from_episode_name(episode):
        splitted_episode = episode.split('.')
        return " ".join(splitted_episode[:-1]), splitted_episode[-1]

    def get_headers_with_user_agent(self):
        headers = requests.utils.default_headers()
        headers.update({'User-Agent': self.user_agent.random})
        return headers


class MoviesDownloader(BaseDownloader):
    def __init__(self):
        super(MoviesDownloader, self).__init__()
        self.tc = transmissionrpc.Client(user="transmission", password="transmission")

    def get_pirate_bay_soup(self, episode):
        pirate_url = "https://thepiratebay.org/search/{episode}/0/99/0".format(episode=episode)
        print "Trying to reach: %s" % pirate_url
        self.session.visit(pirate_url)
        try:
            self.session.wait_for(lambda: self.session.at_css("td.vertTh"))
        except Exception, e:
            print "Failed while trying to reach piratebay.org: %s" % e
            return None
        response = self.session.body()
        soup = BeautifulSoup(response, "lxml")
        return soup

    def extract_magnet_link_from_soup(self, soup, episode, best_resolution):
        print "Extracting magnet link for episode: %s" % episode
        data = self.extract_results(soup)
        status, magnet_link = self.find_best_result(data, episode, best_resolution)
        return status, magnet_link

    @staticmethod
    def extract_results(soup):
        table = soup.find('table', attrs={'id': 'searchResult'})
        data = []
        if table:
            rows = table.find_all('tr')
            for row in rows[1:]:
                cols = row.find_all('td')
                cols_text = [ele.text.strip() for ele in cols]
                cols_text.extend([None, None])
                for col in cols:
                    magnet_link = col.find('a', href=MAGNET_REGEX)
                    is_valid = col.find(attrs={'title': ['VIP', 'Trusted']}) is not None
                    if is_valid:
                        cols_text[5] = is_valid
                    if magnet_link:
                        cols_text[4] = magnet_link.get('href')
                data.append(cols_text)
        return data

    @staticmethod
    def find_best_result(data, episode, best_resolution):
        status = Status.no_good_results if data else Status.no_results
        proper_results = list()
        for row in data:
            _, name, se, le, magnet_link, is_valid = row
            if magnet_link and (episode in name.lower() or episode.replace('.', ' ') in name.lower()) \
                and ((int(se) > SEEKER_THRESHOLD and int(le) > LEECHES_THRESHOLD)
                     or (is_valid and int(se) > SEEKER_THRESHOLD // 5)):
                size = int(SERIES_SIZE_REGEX.search(name.lower()).group(1))
                proper_results.append((magnet_link, size, row))
        if proper_results:
            if best_resolution:
                best_result = max(proper_results, key=lambda x: x[1])
            else:
                best_result = max(proper_results, key=lambda x: int(x[2][2]))
            _, name, se, le, magnet_link, is_valid = best_result[2]
            print u"Found {trusted} result: {name} with {seeders} seeders, " \
                  u"{leechers} leechers".format(name=name, seeders=se, leechers=le,
                                                trusted="trusted" if is_valid else "not trusted")

            return Status.success, best_result[0]
        return status, None

    def download_torrent_from_magnet_link(self, magnet_link, download_directory):
        torrent = self.tc.add_torrent(magnet_link, download_dir=download_directory)
        bar = progressbar.ProgressBar(widgets=[progressbar.Percentage(), progressbar.Bar()]).start()
        while not hasattr(torrent, "status") or torrent.status == "downloading":
            time.sleep(1)
            torrent.update()
            bar.update(torrent.progress)
        bar.finish()
        print "Done downloading the link"
        return torrent.name

    def download_torrent(self, series, season_number, episode_number, download_directory, best_resolution):
        episode = self.get_episode_name(series, season_number, episode_number)
        series, episode_details = self.extract_details_from_episode_name(episode)
        self.set_crawling_attributes()
        soup = self.get_pirate_bay_soup(episode)
        if soup:
            status, magnet_link = self.extract_magnet_link_from_soup(soup, episode, best_resolution)
            if magnet_link:
                download_name = self.download_torrent_from_magnet_link(magnet_link, download_directory)
                if download_name:
                    download_version = re.search(VERSION_REGEX_PATTERN.format(series=series.replace(' ', '.'),
                                                                              episode_details=episode_details),
                                                 download_name, re.I).group(1)
                    return Status.success, download_version
            else:
                if status == Status.no_results:
                    print "Couldn't find any matching episode to: %s" % episode
                else:
                    print "Couldn't find any trusty episode to: %s" % episode
                return status, None
        else:
            return Status.no_connection, None
        return Status.generic_erorr, None


class SubtitlesDownloader(BaseDownloader):
    def __init__(self):
        super(SubtitlesDownloader, self).__init__()

    def stream_download_subtitles(self, download_link, download_directory, referrer_link=None):
        headers = self.get_headers_with_user_agent()
        if referrer_link:
            headers['referer'] = referrer_link
            headers['authority'] = AUTHORITY_REGEX.search(download_link).group(1)
        response = requests.get(download_link, stream=True, headers=headers)
        if response.ok:
            z = zipfile.ZipFile(StringIO.StringIO(response.content))
            z.extractall(download_directory)
            print "Subtitles extracted in: %s" % download_directory
            return Status.success
        else:
            print "Failed while trying to download the subtitles"
            return Status.generic_erorr

    def get_soup(self, series, season_number, episode_number, lang="en"):
        raise NotImplementedError

    def get_download_link(self, soup, episode, download_version):
        raise NotImplementedError

    def download_subtitles(self, series, season_number, episode_number, download_version,
                           download_directory, lang="en"):
        episode = self.get_episode_name(series, season_number, episode_number)
        self.set_crawling_attributes()
        soup = self.get_soup(series, season_number, episode_number, lang=lang)
        if soup:
            download_link, referrer_link = self.get_download_link(soup, episode, download_version)
            if download_link:
                return self.stream_download_subtitles(download_link, download_directory, referrer_link=referrer_link)


class OpenSubtitleDownloader(SubtitlesDownloader):
    def __init__(self):
        super(OpenSubtitleDownloader, self).__init__()

    def get_soup(self, series, season_number, episode_number, lang="en"):
        opensubtitles_url = "https://www.opensubtitles.org/en/search/sublanguageid-all/searchonlytvseries-on/" \
                            "season-{season_number}/episode-{episode}/fulltextuseor-on/moviename-{series}/" \
                            "sublanguageid-{lang}" \
            .format(series=series.replace(' ', '-'), season_number=season_number,
                    episode=episode_number, lang=CANONIZE_LANG.get(lang, lang))
        print "Trying to reach: %s" % opensubtitles_url
        self.session.visit(opensubtitles_url)
        try:
            self.session.wait_for(lambda: self.session.at_css("div.msg"))
        except Exception, e:
            print "Failed while trying to download the subtitles: %s" % e
            return None
        response = self.session.body()
        soup = BeautifulSoup(response, "lxml")
        return soup

    def get_download_link(self, soup, episode, download_version):
        print "Extracting download link for episode: %s" % episode
        subtitles = soup.find_all('td', {"class": ["sb_star_odd", "sb_star_even"]})
        download_id = None
        referrer_link = None
        final_download_version = None
        series, episode_details = self.extract_details_from_episode_name(episode)
        highest_similarity = -1
        for subtitle in subtitles:
            subtitles_text = subtitle.text.lower()
            if '"{series}"'.format(series=series) in subtitles_text and episode_details in subtitles_text:
                result = re.search(VERSION_REGEX_PATTERN.format(series=series.replace(' ', '.'),
                                                                episode_details=episode_details), subtitles_text)
                version = result.group(1) if result else u""
                similarity = Levenshtein.ratio(version.lower(), download_version.lower())
                if similarity > highest_similarity:
                    highest_similarity = similarity
                    final_download_version = version
                    download_id = OPENSUBTITLES_DOWNLOAD_REGEX.search(subtitle.find("a").get("onclick")).group(1)
                    referrer_link = "%s%s" % (OPENSUBTITLES_BASE_URL,
                                              OPENSUBTITLES_REFERRER_REGEX
                                              .search(subtitle.find("a").get("onclick")).group(1))
        if download_id:
            download_link = "https://dl.opensubtitles.org/en/download/sub/{download_id}" \
                .format(download_id=download_id)
            print "Found subtitles of version: %s, with download_link: %s" % (final_download_version, download_link)
            return download_link, referrer_link
        print "Couldn't find any matching subtitles to: %s" % episode
        return None, None


class SubscenterDownloader(SubtitlesDownloader):
    def __init__(self):
        super(SubscenterDownloader, self).__init__()

    def get_soup(self, series, season_number, episode_number, **kwargs):
        subscenter_url = "http://www.subscenter.org/he/subtitle/series/{series}/{season_number}/{episode_number}/" \
            .format(series=series.replace(' ', '-'), season_number=season_number, episode_number=episode_number)
        print "Trying to reach: %s" % subscenter_url
        self.session.visit(subscenter_url)
        try:
            self.session.wait_for(lambda: self.session.at_css("div.subsDownloadVersion"))
        except Exception, e:
            print "Failed while trying to download the subtitles: %s" % e
            return None
        response = self.session.body()
        soup = BeautifulSoup(response, "lxml")
        return soup

    def get_download_link(self, soup, episode, download_version):
        print "Extracting download link for episode: %s" % episode
        series, episode_details = self.extract_details_from_episode_name(episode)
        buttons_text = soup.find_all("div", {"class": "subsDownloadBtn"})
        versions = soup.find_all("div", {"class": "subsDownloadVersion"})
        download_id = None
        final_download_version = None
        highest_similarity = -1
        for button_text, version in zip(buttons_text, versions):
            result = re.search(VERSION_REGEX_PATTERN.format(series=series.replace(' ', '.'),
                                                            episode_details=episode_details), version.text, re.I)
            if result:
                version = result.group(1)
                similarity = Levenshtein.ratio(version.lower(), download_version.lower())
                if similarity > highest_similarity:
                    highest_similarity = similarity
                    final_download_version = version
                    download_id = SUBSCENTER_DOWNLOAD_REGEX.search(button_text.find("a").get("onclick")).group(1)
        if download_id:
            download_link = "http://www.subscenter.org/he/get/download/he/?{download_id}"\
                .format(download_id=download_id)
            print "Found subtitles of version: %s" % final_download_version
            return download_link, None
        print "Couldn't find any matching subtitles to: %s" % episode
        return None, None


def create_directory(directory):
    if not os.path.exists(directory):
        os.mkdir(directory)
        os.chmod(directory, 0777)


def run(series, season_number, episode_number, download_directory, lang, full_season=False,
        should_use_subscenter=False, subtitles_only=False, best_resolution=False, **kwargs):
    season_number = str(season_number).zfill(2)
    episode_number = episode_number if isinstance(episode_number, int) \
        else int(episode_number) if episode_number.isdigit() else 0
    if not full_season:
        episodes_numbers = [episode_number]
    else:
        episodes_numbers = range(max(episode_number, 1), MAX_NUMBER_OF_EPISODE_PER_SEASON)

    for component in [series.replace(' ', '-'), "season%s" % season_number]:
        download_directory = os.path.join(download_directory, component)
        create_directory(download_directory)
    series = series.lower()
    movies_downloader = MoviesDownloader()
    subscenter_downloader = SubscenterDownloader()
    opensubtitles_downloader = OpenSubtitleDownloader()

    episodes_directories = list()
    downloaded_episodes = list()
    for episode_number in episodes_numbers:
        status = None
        download_version = u""
        while not status or status == Status.no_connection:
            episode_number = str(episode_number).zfill(2)
            episode_download_directory = os.path.join(download_directory, "episode%s" % episode_number)
            if episode_download_directory not in episodes_directories:
                create_directory(episode_download_directory)
                episodes_directories.append(episode_download_directory)
            if not subtitles_only:
                status, download_version = movies_downloader.download_torrent(series, season_number, episode_number,
                                                                              episode_download_directory,
                                                                              best_resolution)
            else:
                status = Status.success
            if download_version is not None:
                subtitles_status = None
                if should_use_subscenter and lang == HEBREW:
                    subtitles_status = subscenter_downloader.download_subtitles(series, season_number, episode_number,
                                                                                download_version,
                                                                                episode_download_directory)
                if (not subtitles_status or subtitles_status != Status.success) and lang == HEBREW:
                    subtitles_status = opensubtitles_downloader.download_subtitles(series, season_number,
                                                                                   episode_number, download_version,
                                                                                   episode_download_directory,
                                                                                   lang=lang)
                if not subtitles_status or subtitles_status != Status.success:
                    opensubtitles_downloader.download_subtitles(series, season_number, episode_number,
                                                                download_version, episode_download_directory,
                                                                lang=ENGLISH)
            if status == Status.no_connection:
                time.sleep(60)
        if status == Status.no_results:
            break
        elif status == Status.success:
            downloaded_episodes.append(episode_number)

    for directory in episodes_directories:
        if not os.listdir(directory):
            os.rmdir(directory)
        else:
            files = glob.glob(os.path.join(directory, "*.nfo"))
            for file_path in files:
                os.remove(file_path)
    print "The following episodes: %s were downloaded successfully" % downloaded_episodes

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('-sea', '--season_number')
    parser.add_argument('-ser', '--series')
    parser.add_argument('-e', '--episode_number')
    parser.add_argument('-d', '--download_directory')
    parser.add_argument('-l', '--lang')
    parser.add_argument('-f', '--full_season')
    args = parser.parse_args()
    run(**args.__dict__)
