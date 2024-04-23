#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

import pikepdf
import ocrmypdf
from pdfminer.high_level import extract_text
from watchdog.events import PatternMatchingEventHandler
from watchdog.observers import Observer
from watchdog.observers.polling import PollingObserver

# pylint: disable=logging-format-interpolation

def getenv_bool(name: str, default: str = 'False'):
    return os.getenv(name, default).lower() in ('true', 'yes', 'y', '1')

INPUT_DIRECTORY = os.getenv('OCR_INPUT_DIRECTORY', 'scan-input')
OUTPUT_DIRECTORY = os.getenv('OCR_OUTPUT_DIRECTORY', 'ocr-output')
ARCHIVE_DIRECTORY = os.getenv('OCR_ARCHIVE_DIRECTORY', '/processed')
OUTPUT_DIRECTORY_YEAR_MONTH = getenv_bool('OCR_OUTPUT_DIRECTORY_YEAR_MONTH')
ON_SUCCESS_DELETE = getenv_bool('OCR_ON_SUCCESS_DELETE')
ON_SUCCESS_ARCHIVE = getenv_bool('OCR_ON_SUCCESS_ARCHIVE')
DESKEW = getenv_bool('OCR_DESKEW')
OCR_JSON_SETTINGS = json.loads(os.getenv('OCR_JSON_SETTINGS', '{}'))
POLL_NEW_FILE_SECONDS = int(os.getenv('OCR_POLL_NEW_FILE_SECONDS', '1'))
USE_POLLING = getenv_bool('OCR_USE_POLLING')
RETRIES_LOADING_FILE = int(os.getenv('OCR_RETRIES_LOADING_FILE', '5'))
LOGLEVEL = os.getenv('OCR_LOGLEVEL', 'INFO')
PATTERNS = ['*.pdf', '*.PDF']

log = logging.getLogger('ocrmypdf-watcher')

def get_output_dir(root, basename):
    if OUTPUT_DIRECTORY_YEAR_MONTH:
        today = datetime.today()
        output_directory_year_month = Path(root) / str(today.year) / f'{today.month:02d}'
        if not output_directory_year_month.exists():
            output_directory_year_month.mkdir(parents=True, exist_ok=True)
        output_path = output_directory_year_month / basename
    else:
        output_path = Path(OUTPUT_DIRECTORY) / basename
    return output_path

def wait_for_file_ready(file_path):
    retries = RETRIES_LOADING_FILE
    while retries:
        try:
            pdf = pikepdf.open(file_path)
        except (FileNotFoundError, pikepdf.PdfError) as e:
            log.info(f"[watcher] File {file_path} is not ready yet")
            log.debug("[watcher] Exception was", exc_info=e)
            time.sleep(POLL_NEW_FILE_SECONDS)
            retries -= 1
        else:
            pdf.close()
            return True
    return False

def execute_ocrmypdf(file_path):
    file_path = Path(file_path)
    output_path = get_output_dir(OUTPUT_DIRECTORY, file_path.name)

    log.info("[watcher] " + "-" * 20)
    log.info(f'[watcher] New file: {file_path}. Waiting until fully loaded...')
    if not wait_for_file_ready(file_path):
        log.info(f"[watcher] Gave up waiting for {file_path} to become ready")
        return
    log.info(f'[watcher] Attempting to OCRmyPDF to: {output_path}')

    creation_time = file_path.stat().st_mtime
    modification_time = file_path.stat().st_mtime
    
    exit_code = ocrmypdf.ocr(
        input_file=file_path,
        output_file=output_path, 
        deskew=DESKEW,
        **OCR_JSON_SETTINGS,
    )
    if exit_code == 0:
        os.utime(output_path, (creation_time, modification_time))

        if ON_SUCCESS_DELETE:
            log.info(f'[watcher] OCR is done. Deleting: {file_path}')
            file_path.unlink()
        elif ON_SUCCESS_ARCHIVE:
            log.info(f'[watcher] OCR is done. Archiving {file_path.name} to {ARCHIVE_DIRECTORY}')
            archive_path = f'{ARCHIVE_DIRECTORY}/{file_path.name}'
            shutil.move(file_path, archive_path)
            os.utime(archive_path, (creation_time, modification_time))
        else:
            log.info('[watcher] OCR is done')
    else:
        log.info('[watcher] OCR is done')

def autocorrect_match(match):
    match = match.replace(" ", "")

    if match.startswith('P0-'):
        match = 'PO' + match[2:]
    elif match.startswith('PQ-'):
        match = 'PO' + match[2:]
    elif match.startswith('RNW-'):
        match = 'RNWS' + match[3:]
    elif match.startswith('5P0-'):
        match = 'SPO' + match[3:]
    elif match.startswith('56R-'):
        match = 'SGR' + match[3:]

    parts = re.match(r'([A-Z]+)[-]?(\d{1,2})[-]?(\d{1,4})', match)

    if parts is not None:
        prefix = parts.group(1)
        second_part = parts.group(2).zfill(2)
        last_part = parts.group(3).zfill(4)

        second_part = second_part.replace("O", "0").replace("I", "1").replace("S", "5").replace("B", "8").replace("Z", "2").replace("G", "6")
        last_part = last_part.replace("O", "0").replace("I", "1").replace("S", "5").replace("B", "8").replace("Z", "2").replace("G", "6")

        if prefix in ["PO", "SPO", "RNWS", "SGR", "SSR"]:
            second_part = "2" + second_part[1:]

        corrected = f"{prefix}-{second_part}-{last_part}"
        return corrected
    else:
        return match

class HandleObserverEvent(PatternMatchingEventHandler):
    def on_any_event(self, event):
        if event.event_type in ['created']:
            execute_ocrmypdf(event.src_path)
            self.process_pdf(event.src_path)

    def process_pdf(self, path):
        if path.endswith('.pdf'):
            print(f'[renamer] Processing file: {path}')

            time.sleep(3)

            try:
                text = extract_text(path)
                matches = re.findall(r'(?:P0|PO|SPO|RNWS|SGR|SSR) ?\d?-?\d{1,2}-\d{1,4}', text, re.IGNORECASE)
                matches = [match.upper() for match in matches]

                if matches:
                    matches = [autocorrect_match(match) for match in matches]
                    matches = list(set(matches))
                    matches.sort()
                final_name = '_'.join(matches) + '.pdf'
                max_length = 150 - 4
                if len(final_name) > max_length:
                    final_name = final_name[:max_length] + '.pdf'

                if os.path.exists(os.path.join('final-output', final_name)):
                    num = 1
                    while os.path.exists(os.path.join('final-output', final_name[:-4] + f'({num}).pdf')):
                        num += 1
                    final_name = final_name[:-4] + f'({num}).pdf'
                os.rename(path, os.path.join(os.path.dirname(path), final_name))
                shutil.move(os.path.join(os.path.dirname(path), final_name), 'final-output')
                print(f'[renamer] Processed and moved file: {final_name}')
                else:
                shutil.move(path, os.path.join('final-output', os.path.basename(path)))
                print(f'[renamer] No pattern found, moved file to final-output folder: {path}')
            except Exception as e:
                print(f'[renamer] Error processing file: {path}. Error: {e}')
                if os.path.exists(path):
                    if not os.path.exists('ERROR'):
                        os.mkdir('ERROR')
                    shutil.move(path, os.path.join('ERROR', os.path.basename(path)))
                    print(f'[renamer] Moved file with error to ERROR folder: {path}')


def main():
    ocrmypdf.configure_logging(
        verbosity=(
            ocrmypdf.Verbosity.default
            if LOGLEVEL != 'DEBUG'
            else ocrmypdf.Verbosity.debug
        ),
        manage_root_logger=True,
    )
    log.setLevel(LOGLEVEL)
    log.info(
        f"[watcher] Starting OCRmyPDF watcher with config:\n"
        f"Input Directory: {INPUT_DIRECTORY}\n"
        f"Output Directory: {OUTPUT_DIRECTORY}\n"
        f"Output Directory Year & Month: {OUTPUT_DIRECTORY_YEAR_MONTH}\n"
        f"Archive Directory: {ARCHIVE_DIRECTORY}"
    )
    log.debug(
        f"[watcher] INPUT_DIRECTORY: {INPUT_DIRECTORY}\n"
        f"OUTPUT_DIRECTORY: {OUTPUT_DIRECTORY}\n"
        f"OUTPUT_DIRECTORY_YEAR_MONTH: {OUTPUT_DIRECTORY_YEAR_MONTH}\n"
        f"ARCHIVE_DIRECTORY: {ARCHIVE_DIRECTORY}\n"
        f"ON_SUCCESS_DELETE: {ON_SUCCESS_DELETE}\n"
        f"ON_SUCCESS_ARCHIVE: {ON_SUCCESS_ARCHIVE}\n"
        f"DESKEW: {DESKEW}\n"
        f"ARGS: {OCR_JSON_SETTINGS}\n"
        f"POLL_NEW_FILE_SECONDS: {POLL_NEW_FILE_SECONDS}\n"
        f"RETRIES_LOADING_FILE: {RETRIES_LOADING_FILE}\n"
        f"USE_POLLING: {USE_POLLING}\n"
        f"LOGLEVEL: {LOGLEVEL}"
    )
    if 'input_file' in OCR_JSON_SETTINGS or 'output_file' in OCR_JSON_SETTINGS:
        log.error('[watcher] OCR_JSON_SETTINGS should not specify input file or output file')
        sys.exit(1)

    handler = HandleObserverEvent(patterns=PATTERNS)
    if USE_POLLING:
        observer = PollingObserver()
    else:
        observer = Observer()
    observer.schedule(handler, INPUT_DIRECTORY, recursive=True)
    observer.start()
    print(f'[renamer] Watching folder: {OUTPUT_DIRECTORY}')
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()

if __name__ == "__main__":
    main()
