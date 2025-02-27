#!/usr/bin/env python3
import argparse
import os
import re
import urllib.parse
import urllib.request
import sys
import tempfile
import mimetypes
from pathlib import Path
import time

try:
    from tqdm import tqdm
    HAS_TQDM = True
except ImportError:
    HAS_TQDM = False
    print("tqdm not installed, progress bar will not be shown")

try:
    import pyexiv2
    HAS_EXIV2 = True
except ImportError:
    HAS_EXIV2 = False
    print("pyexiv2 not installed, no metadata will be injected into images")

try:
    import eyed3
    HAS_EYED3 = True
except ImportError:
    HAS_EYED3 = False
    print("eyed3 not installed, no metadata will be injected into audio files")


class FurAffinitySearchDownloader:
    def __init__(self, args):
        self.outdir = args.outdir
        self.use_https = not args.insecure
        self.metadata = not args.plain
        self.rename = not args.no_rename
        self.max_files = args.max_files
        self.max_duplicates = args.max_duplicates
        self.overwrite = args.overwrite
        self.text_meta = args.separate_meta
        self.classic = args.classic
        self.perpage = args.perpage
        self.cookie_file = args.cookie_file
        self.search_query = args.search_query
        
        # Create output directory if it doesn't exist
        os.makedirs(self.outdir, exist_ok=True)
        
        # Set up session headers
        self.headers = {
            'User-Agent': 'Mozilla/5.0 furaffinity-search-dl (Python)'
        }
        
        # Set up cookies if provided
        self.cookies = {}
        if self.cookie_file:
            self._load_cookies()

    def _load_cookies(self):
        try:
            with open(self.cookie_file, 'r') as f:
                for line in f:
                    if line.startswith('#') or line.strip() == '':
                        continue
                    fields = line.strip().split('\t')
                    if len(fields) >= 7:
                        domain, _, path, secure, expires, name, value = fields[:7]
                        if 'furaffinity.net' in domain:
                            self.cookies[name] = value
            print(f"Loaded cookies from {self.cookie_file}")
        except Exception as e:
            print(f"Error loading cookies: {e}")
            sys.exit(1)

    def _make_request(self, url):
        parsed = urllib.parse.urlparse(url)
        encoded_path = urllib.parse.quote(parsed.path, safe='/:')
        encoded_url = parsed._replace(path=encoded_path).geturl()
        
        request = urllib.request.Request(encoded_url, headers=self.headers)
        
        if self.cookies:
            cookie_str = '; '.join([f"{k}={v}" for k, v in self.cookies.items()])
            request.add_header('Cookie', cookie_str)
            
        try:
            return urllib.request.urlopen(request)
        except urllib.error.HTTPError as e:
            if e.code == 403 or e.code == 401:
                print(f"Error: Authentication failed. Please check your cookies. ({e.code})")
                sys.exit(1)
            else:
                print(f"HTTP Error: {e.code} - {e.reason} for URL: {url}")
                return None
        except Exception as e:
            print(f"Error accessing {url}: {e}")
            return None

    def _download_file(self, url, file_path):
        if os.path.exists(file_path) and not self.overwrite:
            print(f"File already exists, skipping: {file_path}")
            return False
            
        try:
            response = self._make_request(url)
            if not response:
                return False
                
            file_size = int(response.headers.get('Content-Length', 0))
            
            with open(file_path, 'wb') as out_file:
                if HAS_TQDM and file_size > 0:
                    with tqdm(total=file_size, unit='B', unit_scale=True, desc=os.path.basename(file_path)) as pbar:
                        while True:
                            chunk = response.read(8192)
                            if not chunk:
                                break
                            out_file.write(chunk)
                            pbar.update(len(chunk))
                else:
                    print(f"Downloading: {os.path.basename(file_path)}")
                    out_file.write(response.read())
            
            return True
            
        except Exception as e:
            print(f"Error downloading {url}: {e}")
            if os.path.exists(file_path):
                os.remove(file_path)
            return False

    def _extract_next_page_url(self, html_content):
        if self.classic:
            match = re.search(r'<a class="button-link right" href="([^"]+)">Next &nbsp;&#x276f;&#x276f;</a>', html_content)
        else:
            match = re.search(r'<form action="([^"]+)"[^>]*>\s*<button[^>]*type="submit">Next', html_content)
        
        if match:
            return "https://www.furaffinity.net" + match.group(1)
        return None

    def _extract_artwork_urls(self, html_content):
        return re.findall(r'<a href="(/view/\d+/)"', html_content)

    def _extract_image_url(self, html_content):
        match = re.search(r'href="//d\.furaffinity\.net/art/[^"]+"', html_content)
        if match:
            url = match.group(0)[6:-1]  # Remove 'href="' and '"'
            protocol = "https:" if self.use_https else "http:"
            return protocol + url
        return None

    def _extract_metadata(self, html_content):
        desc_match = re.search(r'og:description" content="([^"]*)"', html_content)
        description = desc_match.group(1) if desc_match else ""
        
        if self.classic:
            title_match = re.search(r'<h2>(.*?)</h2>', html_content, re.DOTALL)
        else:
            title_match = re.search(r'<h2><p>(.*?)</p></h2>', html_content, re.DOTALL)
        
        title = title_match.group(1) if title_match else "Untitled"
        
        artist_matches = re.finditer(r'<a href="/user/([^/"]+)/">', html_content)
        artist = "unknown_artist"
        for match in artist_matches:
            username = match.group(1)
            if username != "your username":
                artist = username
                break
        
        return title, description, artist

    def _add_metadata(self, file_path, title, description):
        if not self.metadata:
            return
            
        mime_type, _ = mimetypes.guess_type(file_path)
        
        if self.text_meta:
            with open(f"{file_path}.meta", 'w', encoding='utf-8') as f:
                f.write(f"Title: {title}\nURL: {file_path}\nDescription: {description}")
        
        if mime_type and mime_type.startswith('audio') and HAS_EYED3:
            try:
                audio_file = eyed3.load(file_path)
                if audio_file and audio_file.tag:
                    audio_file.tag.title = title
                    if description:
                        audio_file.tag.comments.set(description)
                    audio_file.tag.save()
            except Exception as e:
                print(f"Error adding audio metadata: {e}")
        
        elif mime_type and mime_type.startswith('image') and HAS_EXIV2:
            try:
                metadata = pyexiv2.ImageMetadata(file_path)
                metadata.read()
                
                # Different image formats use different metadata keys
                if mime_type == 'image/jpeg':
                    metadata['Exif.Image.ImageDescription'] = description
                    metadata['Exif.Image.XPTitle'] = title
                elif mime_type == 'image/png':
                    metadata['Xmp.dc.title'] = title
                    metadata['Xmp.dc.description'] = description
                
                metadata.write()
            except Exception as e:
                print(f"Error adding image metadata: {e}")

    def download_search_results(self):
        encoded_query = urllib.parse.quote(self.search_query)
        base_url = f"https://www.furaffinity.net/search/?q={encoded_query}&perpage={self.perpage}&order-by=date&order-direction=desc&page=1"
        
        download_count = 0
        duplicate_count = 0
        page_num = 1
        
        print(f"Searching for: \"{self.search_query}\"")
        print(f"Starting download from: {base_url}")
        
        while True:
            print(f"Processing search page {page_num}...")
            url = f"https://www.furaffinity.net/search/?q={encoded_query}&perpage={self.perpage}&order-by=date&order-direction=desc&page={page_num}"
            
            response = self._make_request(url)
            if not response:
                break
                
            html_content = response.read().decode('utf-8')
            
            if "/login/" in response.url and self.cookie_file:
                print("ERROR: Invalid or expired cookies. Please log in to FurAffinity and export new cookies.")
                sys.exit(1)
            
            if "No results found" in html_content:
                if page_num == 1:
                    print(f"No search results found for \"{self.search_query}\".")
                    return
                else:
                    print("No more search results found.")
                    break
            
            artwork_pages = self._extract_artwork_urls(html_content)
            if not artwork_pages:
                print(f"No artwork links found on page {page_num}. This might be the last page.")
                break
            
            for page_path in artwork_pages:
                page_url = f"https://www.furaffinity.net{page_path}"
                print(f"Processing submission: {page_url}")
                
                artwork_response = self._make_request(page_url)
                if not artwork_response:
                    continue
                    
                artwork_html = artwork_response.read().decode('utf-8')
                
                # Skip if system message (inaccessible)
                if "System Message" in artwork_html:
                    print(f"WARNING: {page_path} seems to be inaccessible, skipping.")
                    continue
                
                image_url = self._extract_image_url(artwork_html)
                if not image_url:
                    print(f"WARNING: Could not find download URL for {page_path}, skipping.")
                    continue
                
                title, description, artist = self._extract_metadata(artwork_html)
                
                artist_dir = os.path.join(self.outdir, artist)
                os.makedirs(artist_dir, exist_ok=True)
                
                file_ext = os.path.splitext(image_url)[1]
                file_name = os.path.basename(image_url)
                
                if self.rename:
                    safe_title = re.sub(r'[^A-Za-z0-9._-]', ' ', title)
                    file_path = os.path.join(artist_dir, f"{safe_title}{file_ext}")
                else:
                    file_path = os.path.join(artist_dir, file_name)
                
                if os.path.exists(file_path) and not self.overwrite:
                    print(f"File already exists, skipping: {file_path}")
                    duplicate_count += 1
                    if self.max_duplicates > 0 and duplicate_count >= self.max_duplicates:
                        print(f"Reached set maximum of consecutive duplicate files ({self.max_duplicates})")
                        return
                else:
                    success = self._download_file(image_url, file_path)
                    if success:
                        duplicate_count = 0
                        download_count += 1
                        
                        # Add metadata to the file
                        self._add_metadata(file_path, title, description)
                        
                        # Check if we've reached the download limit
                        if self.max_files > 0 and download_count >= self.max_files:
                            print(f"Reached set file download limit ({self.max_files}).")
                            return
                
                time.sleep(0.5)
            
            # Move to next page
            page_num += 1
            
        print("Search download complete!")


def main():
    parser = argparse.ArgumentParser(description='Download all images matching a search query on FurAffinity')
    
    parser.add_argument('search_query', help='Search query to download results for')
    parser.add_argument('-o', '--outdir', default='.', help='Output directory for downloaded files')
    parser.add_argument('-i', '--insecure', action='store_true', help='Use HTTP instead of HTTPS')
    parser.add_argument('-c', '--cookie-file', help='Cookie file for restricted content')
    parser.add_argument('-p', '--plain', action='store_true', help='Skip adding metadata to files')
    parser.add_argument('-r', '--no-rename', action='store_true', help="Don't rename files based on titles")
    parser.add_argument('-n', '--max-files', type=int, default=0, help='Maximum number of files to download')
    parser.add_argument('-d', '--max-duplicates', type=int, default=0, help='Maximum consecutive duplicates before exiting')
    parser.add_argument('-w', '--overwrite', action='store_true', help='Overwrite existing files')
    parser.add_argument('-s', '--separate-meta', action='store_true', help='Create separate metadata files')
    parser.add_argument('-t', '--classic', action='store_true', help='Use classic theme selectors')
    parser.add_argument('-l', '--perpage', type=int, default=72, help='Number of results per page')
    
    args = parser.parse_args()
    
    downloader = FurAffinitySearchDownloader(args)
    downloader.download_search_results()


if __name__ == "__main__":
    main()
