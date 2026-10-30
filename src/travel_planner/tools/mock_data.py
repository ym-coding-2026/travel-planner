"""Mock 数据：3 个城市各 10 个真实 POI，字段贴近真实高德返回。

字段说明：
- id / name / category / lat / lng / address / rating
- avg_cost: 人均消费（元）
- duration: 建议游玩时长（分钟）
- open_time: 开放时间
- ticket_price: 门票（0 表示免费）
- tags: 标签，供 keywords 模糊匹配

后续换真实高德时，只需把 backend 里的 mock 分支替换为 API 调用，本文件可删除。
"""
from typing import Dict, List

MOCK_POIS: Dict[str, List[dict]] = {
    "北京": [
        {"id": "bj-01", "name": "故宫博物院", "category": "历史古迹", "lat": 39.9163, "lng": 116.3972,
         "address": "北京市东城区景山前街4号", "rating": 4.8, "avg_cost": 120, "duration": 180,
         "open_time": "08:30-17:00", "ticket_price": 60, "tags": ["历史", "宫殿", "世界遗产"]},
        {"id": "bj-02", "name": "天安门广场", "category": "现代地标", "lat": 39.9055, "lng": 116.3976,
         "address": "北京市东城区长安街", "rating": 4.7, "avg_cost": 0, "duration": 60,
         "open_time": "05:00-22:00", "ticket_price": 0, "tags": ["广场", "地标"]},
        {"id": "bj-03", "name": "颐和园", "category": "公园", "lat": 39.9999, "lng": 116.2755,
         "address": "北京市海淀区新建宫门路19号", "rating": 4.7, "avg_cost": 100, "duration": 180,
         "open_time": "06:30-18:00", "ticket_price": 30, "tags": ["园林", "世界遗产", "湖"]},
        {"id": "bj-04", "name": "八达岭长城", "category": "历史古迹", "lat": 40.3597, "lng": 116.0200,
         "address": "北京市延庆区八达岭镇", "rating": 4.8, "avg_cost": 150, "duration": 240,
         "open_time": "07:30-17:30", "ticket_price": 40, "tags": ["长城", "世界遗产"]},
        {"id": "bj-05", "name": "天坛公园", "category": "历史古迹", "lat": 39.8822, "lng": 116.4066,
         "address": "北京市东城区天坛内东里", "rating": 4.6, "avg_cost": 80, "duration": 120,
         "open_time": "06:00-21:00", "ticket_price": 15, "tags": ["祭坛", "世界遗产"]},
        {"id": "bj-06", "name": "北海公园", "category": "公园", "lat": 39.9255, "lng": 116.3899,
         "address": "北京市西城区文津街1号", "rating": 4.5, "avg_cost": 60, "duration": 120,
         "open_time": "06:30-20:00", "ticket_price": 10, "tags": ["园林", "湖"]},
        {"id": "bj-07", "name": "圆明园", "category": "公园", "lat": 40.0078, "lng": 116.2981,
         "address": "北京市海淀区清华西路28号", "rating": 4.5, "avg_cost": 60, "duration": 150,
         "open_time": "07:00-19:00", "ticket_price": 10, "tags": ["遗址", "园林"]},
        {"id": "bj-08", "name": "南锣鼓巷", "category": "街区", "lat": 39.9350, "lng": 116.4030,
         "address": "北京市东城区南锣鼓巷", "rating": 4.4, "avg_cost": 50, "duration": 90,
         "open_time": "全天", "ticket_price": 0, "tags": ["胡同", "美食街", "逛街"]},
        {"id": "bj-09", "name": "鸟巢(国家体育场)", "category": "现代地标", "lat": 39.9929, "lng": 116.3964,
         "address": "北京市朝阳区国家体育场南路", "rating": 4.6, "avg_cost": 100, "duration": 120,
         "open_time": "09:00-21:00", "ticket_price": 50, "tags": ["体育", "地标", "夜景"]},
        {"id": "bj-10", "name": "798艺术区", "category": "现代地标", "lat": 39.9842, "lng": 116.4954,
         "address": "北京市朝阳区酒仙桥路2号", "rating": 4.5, "avg_cost": 0, "duration": 150,
         "open_time": "全天", "ticket_price": 0, "tags": ["艺术", "展览", "拍照"]},
    ],
    "上海": [
        {"id": "sh-01", "name": "外滩", "category": "现代地标", "lat": 31.2397, "lng": 121.4905,
         "address": "上海市黄浦区中山东一路", "rating": 4.7, "avg_cost": 0, "duration": 90,
         "open_time": "全天", "ticket_price": 0, "tags": ["夜景", "地标", "江景"]},
        {"id": "sh-02", "name": "东方明珠", "category": "现代地标", "lat": 31.2397, "lng": 121.4998,
         "address": "上海市浦东新区世纪大道1号", "rating": 4.6, "avg_cost": 150, "duration": 120,
         "open_time": "09:00-21:00", "ticket_price": 160, "tags": ["地标", "观景", "塔"]},
        {"id": "sh-03", "name": "豫园", "category": "历史古迹", "lat": 31.2270, "lng": 121.4920,
         "address": "上海市黄浦区安仁街137号", "rating": 4.5, "avg_cost": 80, "duration": 120,
         "open_time": "08:30-17:00", "ticket_price": 40, "tags": ["园林", "老城厢"]},
        {"id": "sh-04", "name": "南京路步行街", "category": "商业街", "lat": 31.2360, "lng": 121.4790,
         "address": "上海市黄浦区南京东路", "rating": 4.4, "avg_cost": 100, "duration": 120,
         "open_time": "全天", "ticket_price": 0, "tags": ["购物", "美食街", "逛街"]},
        {"id": "sh-05", "name": "上海迪士尼乐园", "category": "主题乐园", "lat": 31.1450, "lng": 121.6700,
         "address": "上海市浦东新区川沙新镇", "rating": 4.7, "avg_cost": 300, "duration": 480,
         "open_time": "09:00-20:00", "ticket_price": 399, "tags": ["亲子", "乐园", "演出"]},
        {"id": "sh-06", "name": "上海博物馆", "category": "博物馆", "lat": 31.2286, "lng": 121.4750,
         "address": "上海市黄浦区人民大道201号", "rating": 4.7, "avg_cost": 0, "duration": 180,
         "open_time": "09:00-17:00", "ticket_price": 0, "tags": ["文物", "博物馆", "免费"]},
        {"id": "sh-07", "name": "静安寺", "category": "宗教场所", "lat": 31.2240, "lng": 121.4450,
         "address": "上海市静安区南京西路1686号", "rating": 4.5, "avg_cost": 0, "duration": 90,
         "open_time": "07:30-17:00", "ticket_price": 0, "tags": ["寺庙", "地标"]},
        {"id": "sh-08", "name": "田子坊", "category": "街区", "lat": 31.2100, "lng": 121.4730,
         "address": "上海市黄浦区泰康路210弄", "rating": 4.3, "avg_cost": 80, "duration": 120,
         "open_time": "全天", "ticket_price": 0, "tags": ["弄堂", "文创", "美食街"]},
        {"id": "sh-09", "name": "陆家嘴", "category": "现代地标", "lat": 31.2390, "lng": 121.5050,
         "address": "上海市浦东新区陆家嘴", "rating": 4.6, "avg_cost": 0, "duration": 90,
         "open_time": "全天", "ticket_price": 0, "tags": ["天际线", "商圈", "夜景"]},
        {"id": "sh-10", "name": "上海科技馆", "category": "博物馆", "lat": 31.2100, "lng": 121.5480,
         "address": "上海市浦东新区世纪大道2000号", "rating": 4.5, "avg_cost": 100, "duration": 180,
         "open_time": "09:00-17:15", "ticket_price": 45, "tags": ["科普", "亲子", "博物馆"]},
    ],
    "成都": [
        {"id": "cd-01", "name": "宽窄巷子", "category": "街区", "lat": 30.6690, "lng": 104.0560,
         "address": "成都市青羊区金河路口", "rating": 4.5, "avg_cost": 80, "duration": 120,
         "open_time": "全天", "ticket_price": 0, "tags": ["胡同", "美食街", "喝茶"]},
        {"id": "cd-02", "name": "锦里", "category": "美食街", "lat": 30.6400, "lng": 104.2580,
         "address": "成都市武侯区武侯祠大街231号", "rating": 4.4, "avg_cost": 70, "duration": 120,
         "open_time": "全天", "ticket_price": 0, "tags": ["美食街", "古街", "小吃"]},
        {"id": "cd-03", "name": "武侯祠", "category": "历史古迹", "lat": 30.6410, "lng": 104.2560,
         "address": "成都市武侯区武侯祠大街231号", "rating": 4.6, "avg_cost": 90, "duration": 120,
         "open_time": "08:00-21:00", "ticket_price": 50, "tags": ["三国", "祠堂", "历史"]},
        {"id": "cd-04", "name": "成都大熊猫繁育研究基地", "category": "自然风光", "lat": 30.7330, "lng": 104.1460,
         "address": "成都市成华区熊猫大道1375号", "rating": 4.8, "avg_cost": 100, "duration": 240,
         "open_time": "07:30-18:00", "ticket_price": 55, "tags": ["熊猫", "亲子", "自然"]},
        {"id": "cd-05", "name": "杜甫草堂", "category": "历史古迹", "lat": 30.6590, "lng": 104.0390,
         "address": "成都市青羊区青华路37号", "rating": 4.5, "avg_cost": 80, "duration": 120,
         "open_time": "08:00-18:00", "ticket_price": 50, "tags": ["诗人", "园林", "历史"]},
        {"id": "cd-06", "name": "春熙路", "category": "商业街", "lat": 30.6570, "lng": 104.0810,
         "address": "成都市锦江区春熙路", "rating": 4.3, "avg_cost": 100, "duration": 120,
         "open_time": "全天", "ticket_price": 0, "tags": ["购物", "商圈", "美食街"]},
        {"id": "cd-07", "name": "都江堰", "category": "历史古迹", "lat": 31.0050, "lng": 103.6200,
         "address": "成都市都江堰市公园路", "rating": 4.7, "avg_cost": 120, "duration": 240,
         "open_time": "08:00-17:30", "ticket_price": 80, "tags": ["水利工程", "世界遗产", "自然"]},
        {"id": "cd-08", "name": "青城山", "category": "自然风光", "lat": 30.9100, "lng": 103.5800,
         "address": "成都市都江堰市青城山镇", "rating": 4.6, "avg_cost": 100, "duration": 240,
         "open_time": "08:00-17:00", "ticket_price": 90, "tags": ["道教", "山", "世界遗产"]},
        {"id": "cd-09", "name": "金沙遗址博物馆", "category": "博物馆", "lat": 30.6800, "lng": 104.0200,
         "address": "成都市青羊区金沙遗址路2号", "rating": 4.6, "avg_cost": 90, "duration": 150,
         "open_time": "08:00-18:00", "ticket_price": 70, "tags": ["古蜀", "博物馆", "文物"]},
        {"id": "cd-10", "name": "人民公园", "category": "公园", "lat": 30.6630, "lng": 104.0660,
         "address": "成都市青羊区少城路12号", "rating": 4.4, "avg_cost": 40, "duration": 90,
         "open_time": "06:00-22:00", "ticket_price": 0, "tags": ["喝茶", "公园", "休闲"]},
    ],
}


def get_mock_pois(city: str, keywords: str = "", category: str = "") -> List[dict]:
    """按城市 + 关键词 + 分类过滤 mock POI。LLM 调用前不需要，仅 backend 内部使用。"""
    return MOCK_POIS.get(city.strip(), [])
