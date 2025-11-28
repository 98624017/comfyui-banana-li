/omfyUI-ModelScope-API是一个优秀的第三方Comfyui插件，需要将它精简集成进本插件体系。只提取它的的ModelScope 图像描述生成节点（modelscope_image_caption_node）和生图节点、图像编辑节点（modelscope_image_node.py）
1.将原插件里利用openai库的做法改成原请求，只保留基础功能，像什么token保存等复杂机制无需迁移，像心宝❤Banana一样在在comfyui节点里改为一个apikey输入即可。尝试引入batch_size机制，但上限限制为4.
2.将提取后的节点放入到本插件的根目录，有能复用到心宝❤Banana体系节点方法的尽量复用。
3.检查他的生成后控制、随机种体制是否正常，如不正常则按心宝❤Banana的方法借鉴修改。
4.ModelScope-Image的图像编辑节点、ModelScope-Image 生图节点精简只保留一个Qwen/Qwen-Image。ModelScope-Image 生图节点额外增加一个Tongyi-MAI/Z-Image-Turbo 模型。
6.提取后的节点名称，内部名称等按心宝体系重新命名，内部英文名称也需要调整，避免跟部分用户装过ComfyUI-ModelScope-API的冲突。
7.提取后注意__init__.py之类的修改