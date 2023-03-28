import asyncio
from flask import Flask, request, abort
import aiohttp
import traceback
import os
import json
import logging
import requests

app = Flask(__name__)

TIMEOUT = 25 # Herokus is 30, so it's nice to at least get an error message
HEROKU_PIPELINE = os.getenv("HEROKU_PIPELINE")
HEROKU_API_KEY = os.getenv("HEROKU_API_KEY")

if __name__ != '__main__':
    gunicorn_logger = logging.getLogger('gunicorn.error')
    app.logger.handlers = gunicorn_logger.handlers
    app.logger.setLevel(gunicorn_logger.level)


async def get_heroku_endpoints():
    headers = {
        "Accept": "application/vnd.heroku+json; version=3",
        "Authorization": f"Bearer {HEROKU_API_KEY}",
    }

    async with aiohttp.ClientSession() as session:
        async with session.request(
            method="GET",
            url=f"https://api.heroku.com/pipelines/{HEROKU_PIPELINE}/review-apps",
            headers=headers,
        ) as response:
            result = await response.read()

    review_apps = json.loads(result)
    tasks = []
    for review_app in review_apps:
        if review_app["app"] is None:
            continue
        app_id = review_app["app"]["id"]
        tasks.append(
            asyncio.create_task(
                async_request(
                    "GET", f"https://api.heroku.com/apps/{app_id}", headers=headers
                )
            )
        )
    finished, _ = await asyncio.wait(tasks, return_when=asyncio.ALL_COMPLETED)
    endpoints = []
    for task in finished:
        try:
            result = task.result()
        except Exception as e:
            traceback.print_exc()
            continue
        else:
            content, status_code, headers = result
            heroku_app = json.loads(content)
            endpoints.append(heroku_app["web_url"])
    app.logger.info(f"Found {len(endpoints)} heroku review apps")
    return endpoints


async def async_request(method, url, headers={}, data=None, cookies=None):
    async with aiohttp.ClientSession() as session:
        async with session.request(
            method=method,
            url=url,
            headers=headers,
            data=data,
            cookies=cookies,
            timeout=TIMEOUT,
        ) as response:
            return await response.read(), response.status, response.headers.items()

methods = ["GET", "POST", "PATCH", "HEAD", "OPTIONS", "PUT", "DELETE"]

# Reverse proxy route
@app.route("/", defaults={"path": ""}, methods=methods)
@app.route("/<path:path>", methods=methods)
async def reverse_proxy(path):
    endpoints = await get_heroku_endpoints()
    tasks = []
    data = request.get_data()
    app.logger.debug(f"Received webhook {request.method} {path} {data}")
    for endpoint in endpoints:
        url = f"{endpoint}{path}"
        headers = {key: value for (key, value) in request.headers if key != "Host"}
        tasks.append(
            asyncio.create_task(
                async_request(request.method, url, headers, data, request.cookies),
                name=url,
            )
        )
    finished, _ = await asyncio.wait(tasks, return_when=asyncio.ALL_COMPLETED)
    for i, task in enumerate(finished):
        url = task.get_name()
        try:
            result = task.result()
        except Exception as e:
            app.logger.exception(f"Request to {url} failed")
            continue
        else:
            content, status_code, headers = result
            app.logger.info(f"Got {status_code} response from {url}")
            if 200 <= status_code <= 299:
                app.logger.info(f"Forwarding response from upstream {url} - {status_code} {content}")
                return content, status_code, headers
            if i == len(finished)-1:
                app.logger.info(f"No app responded with a 2xx code, returning response from {url} - {status_code} {content}")
                return content, status_code, headers
    app.logger.error("No endpoints, nothing to do.")
    abort(404)


if __name__ == "__main__":
    app.run(debug=True)
