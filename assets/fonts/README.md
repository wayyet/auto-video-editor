# 字体资源目录(Week 4 占位)

Week 4 节点 14/16 会引用本目录的字体文件,但**实际字体文件由运维提供**。

## 缺失影响

- `assets/fonts/SourceHanSansCN-Bold.otf`(中文标题字体)— 缺失时节点 14/16 自动回退到系统字体(`msyhbd.ttc` / `simhei.ttf`)
- `assets/fonts/Roboto-Bold.ttf`(英文字幕字体)— 缺失时同上回退

Week 4 联调不依赖本目录的实际字体文件;生产部署前由运维补齐即可。