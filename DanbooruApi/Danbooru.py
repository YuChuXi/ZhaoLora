import re
from bs4 import BeautifulSoup
import httpx
import random


class Danbooru:

    def __init__(self, proxy: str = "http://127.0.0.1:7890", timeout: int = 30) -> None:
        self.proxy = proxy
        self.timeout = timeout
        self.headers = {
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
            "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
            "cache-control": "no-cache",
            "pragma": "no-cache",
            "sec-ch-ua": '"Not A(Brand";v="99", "Google Chrome";v="121", "Chromium";v="121"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Linux"',
            "sec-fetch-dest": "document",
            "sec-fetch-mode": "navigate",
            "sec-fetch-site": "none",
            "sec-fetch-user": "?1",
            "upgrade-insecure-requests": "1",
            "user-agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
        }
        self.random_c = 20
        # self.R18 = False

    def randomID(self, pageNum: int) -> int:
        return min(abs(random.gauss(0, self.random_c)) * pageNum // 100, pageNum)  # type: ignore

    def setProxy(self, proxy):
        self.proxy = proxy

    async def get(self, url: str) -> httpx.Response:
        print(url)
        async with httpx.AsyncClient(proxy=self.proxy) as client:
            return await client.get(url=url, timeout=self.timeout, headers=self.headers)

    async def getHtml(self, path: str) -> BeautifulSoup:
        req = await self.get(f"https://danbooru.donmai.us/{path}")
        return BeautifulSoup(req.text, "html.parser")

    async def getPost(self, ID: str) -> BeautifulSoup:
        return await self.getHtml(f"post/show/{ID}")

    async def getImage(self, ID: str, path: str) -> None:
        post = await self.getPost(ID)
        imageUrl = post.find("img", attrs={"id": "image"}).get("src")  # type: ignore
        with open(path, "wb") as f:
            f.write((await self.get(imageUrl)).read())  # type: ignore

    async def getImageHD(self, ID: str, path: str) -> None:
        post = await self.getPost(ID)
        try:
            imageUrl = post.find("img", attrs={"id": "highres-show"}).get("herf")  # type: ignore
        except AttributeError:
            imageUrl = post.find("img", attrs={"id": "image"}).get("src")  # type: ignore
        with open(path, "wb") as f:
            f.write((await self.get(imageUrl)).read())  # type: ignore

    async def randomPostID(self, tags: str = "") -> str:
        pageNum = 10000
        if tags != "":
            pageNum = 0
            try:
                pageList = (
                    (await self.getHtml(f"post?tags={tags}"))
                    .find("div", attrs={"class": "pagination"})
                    .find_all("a", attrs={"class": ""})
                )  # type: ignore
                for a in pageList:
                    try:
                        pageNum = max(pageNum, int(a.string))
                    except:
                        pass
            except AttributeError:
                pass

        # p(z)=1+erf(-0.5*z/2^0.5/c)
        pageNum = self.randomID(pageNum)
        postList = (
            await self.getHtml(f"post?page={int(pageNum)}&tags={tags}")
        ).find_all("li", attrs={"class": re.compile("^creator-ID-")})
        if postList == None or len(postList) == 0:
            return ""
        return random.choice(postList).get("id")
