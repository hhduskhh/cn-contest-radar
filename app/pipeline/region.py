"""地域判定：线下赛站在不在广东。

用户只关心广东省内的线下赛站，线上赛事不受影响（天池、DataFountain、讯飞、
AI Studio 这类本来就没有举办地）。

难点在于**赛站标签的形态不统一**：CCPC 有的年份写城市（「国赛（哈尔滨）」），
有的年份写承办学校（「国赛（东北师范大学）」「国赛（武汉大学&长江大学）」）。
所以城市名和高校名都要认。
"""

from __future__ import annotations

# 广东省 21 个地级市
GUANGDONG_CITIES = {
    "广州", "深圳", "珠海", "汕头", "佛山", "韶关", "湛江", "肇庆", "江门",
    "茂名", "惠州", "梅州", "汕尾", "河源", "阳江", "清远", "东莞", "中山",
    "潮州", "揭阳", "云浮",
}

# 广东省内主要高校。赛站标签用学校名时必须靠这个识别，
# 否则「国赛（深圳技术大学）」会被误判成外省。
GUANGDONG_UNIVERSITIES = {
    "深圳技术大学", "深圳大学", "南方科技大学", "哈尔滨工业大学（深圳）",
    "哈尔滨工业大学(深圳)", "香港中文大学（深圳）", "中山大学", "华南理工大学",
    "暨南大学", "华南师范大学", "华南农业大学", "广东工业大学", "广东外语外贸大学",
    "广州大学", "南方医科大学", "广州中医药大学", "广东财经大学", "深圳职业技术大学",
    "东莞理工学院", "佛山大学", "佛山科学技术学院", "汕头大学", "五邑大学",
    "广东海洋大学", "广东技术师范大学", "仲恺农业工程学院", "广东石油化工学院",
    "韶关学院", "嘉应学院", "惠州学院", "肇庆学院", "岭南师范学院",
    "北京师范大学珠海校区", "北京理工大学珠海学院", "吉林大学珠海学院",
    "广州软件学院", "广东科技学院", "深圳信息职业技术学院",
}

# 这些字样说明是线上赛，与地域无关
ONLINE_MARKERS = {
    "网络赛", "网络预选赛", "线上", "在线", "云赛", "网络初赛", "线上赛",
    "网络挑战赛", "远程",
}

REGION_GUANGDONG = "guangdong"
REGION_OTHER = "other"
REGION_ONLINE = "online"


def is_online(*texts: str | None) -> bool:
    joined = " ".join(t for t in texts if t)
    return any(marker in joined for marker in ONLINE_MARKERS)


def is_guangdong(*texts: str | None) -> bool:
    joined = " ".join(t for t in texts if t)
    if any(city in joined for city in GUANGDONG_CITIES):
        return True
    return any(uni in joined for uni in GUANGDONG_UNIVERSITIES)


def classify_station(*texts: str | None) -> str:
    """给线下赛站判地域。线上赛返回 online。"""
    if is_online(*texts):
        return REGION_ONLINE
    if is_guangdong(*texts):
        return REGION_GUANGDONG
    return REGION_OTHER


def classify(source_is_offline: bool, *texts: str | None) -> str:
    """统一入口。texts 传赛站名、标题等一切可能含地名的文本。

    source_is_offline=False 的源（各竞赛平台）一律 online —— 它们没有举办地，
    不该被地域筛选误伤。
    """
    if not source_is_offline:
        return REGION_ONLINE
    return classify_station(*texts)
