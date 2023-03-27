import asyncio
from flask import Flask, request, abort
import aiohttp
import traceback
import os
import json

app = Flask(__name__)

TIMEOUT = 10
HEROKU_PIPELINE = os.getenv("HEROKU_PIPELINE")
HEROKU_API_KEY = os.getenv("HEROKU_API_KEY")


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
            url, content, status_code, headers = result
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
            return url, await response.read(), response.status, response.headers.items()


# Reverse proxy route
@app.route("/", defaults={"path": ""})
@app.route("/<path:path>", methods=None)
async def reverse_proxy(path):
    endpoints = await get_heroku_endpoints()
    tasks = []
    for endpoint in endpoints:
        url = f"{endpoint}{path}"
        headers = {key: value for (key, value) in request.headers if key != "Host"}
        data = request.get_data()
        cookies = request.cookies
        tasks.append(
            asyncio.create_task(
                async_request(request.method, url, headers, data, cookies)
            )
        )
    finished, _ = await asyncio.wait(tasks, return_when=asyncio.ALL_COMPLETED)
    for task in finished:
        try:
            result = task.result()
        except Exception as e:
            traceback.print_exc()
            continue
        else:
            url, content, status_code, headers = result
            app.logger.info(f"Got {status_code} response from {url}")
            if 200 <= status_code <= 299:
                return content, status_code, headers
    abort(404)


if __name__ == "__main__":
    app.run(debug=True)
