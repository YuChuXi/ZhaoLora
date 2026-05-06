
from bs4 import BeautifulSoup
import httpx
from .Danbooru import Danbooru

class KonachanNet(Danbooru):

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        
    async def getHtml(self, path):
        req = await self.get(f"https://konachan.net/{path}")
        # print(req.text)
        print(req.content)
        with open("a.html","wb") as f: f.write(req.content)
        return BeautifulSoup(req.content, 'html.parser')

