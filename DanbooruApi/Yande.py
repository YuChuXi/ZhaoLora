
from bs4 import BeautifulSoup
import httpx
from .Danbooru import Danbooru

class Yande(Danbooru):

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)

    async def getHtml(self, path):
        req = await self.get(f"https://yande.re/{path}")
        # print(req.text)
        return BeautifulSoup(req.content, 'html.parser')

