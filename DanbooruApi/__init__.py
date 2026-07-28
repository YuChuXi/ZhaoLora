from .Danbooru import Danbooru
from .Gelbooru import Gelbooru
from .KonachanCom import KonachanCom
from .KonachanNet import KonachanNet
from .Yande import Yande


proxy = "http://127.0.0.1:7890"

dL: dict[str, Danbooru]={
    "G": Gelbooru(proxy=proxy),
    "C": KonachanCom(proxy=proxy),
    "N": KonachanNet(proxy=proxy),
    "Y": Yande(proxy=proxy)
}

async def getRandomSetu(tags = "", org = "N"):
    return await dL[org].randomPostID(tags=tags)
    path = f"{org}{ID}.jpg"
    await dL[org].getImageHD(ID, path)
    return ID

async def getSetu(ID, org):
    path = f"{org}{ID}.jpg"
    await dL[org].getImageHD(ID, path)
    return ID

async def getSetuToFile(ID, org, path):
    await dL[org].getImageHD(ID, path)
    return ID

def setProxies(proxies):
    for (k, v) in dL.items():
        v.setProxy(proxies)