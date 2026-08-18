"""意图 Router 的窄边界识别模式。"""

import re

CIVIL_DOMAIN_PATTERN = re.compile(r"国考|省考|公务员|行测|申论|考公|事业单位|报名|考试|招录")
SITE_ADDRESS_QUESTION_PATTERN = re.compile(
    r"^\s*[^，。！？!?]{1,40}的(?:官网|官方网站|网站|网址)(?:是|为)?(?:什么|多少|哪个|在哪里)?[？?]?\s*$"
)
