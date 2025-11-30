xinbao_modelscope.py下的Xinbao ModelScope 图像描述节点，现采用的是免费的魔搭渠道，baseurl是：https://api-inference.modelscope.cn,保持该节点的openai兼容格式请求体和请求端点，增加心宝❤Banana节点同款渠道apibaseurl。在Comfyui上做渠道切换选项，原魔搭渠道（选项写为“魔搭社区”）和心宝❤Banana节点同款渠道（选项写为香蕉同款渠道），apikey增加一行为双行，区分为魔搭社区key和香蕉渠道key.在选择香蕉同款渠道时，模型名称可选项为[gemini-3-pro-preview-c,gemini-2.5-pro-c,qwen3-vl-235b-a22b-thinking,qwen3-vl-235b-a22b-instruct],魔搭社区可用模型按现节点现状。节点默认为香蕉同款渠道(baseurl按照心宝❤Banana里解密后的香港专线，连接失败按→美国直连→cf专线顺序重试)


安全的优化代码。看这份审查报告，需要自动化回归测试。安全第一
1. /home/runner/work/comfyui-banana-pro/comfyui-banana-pro/Gemini_Imagen_Generator.py (得分: 54.19)
问题分类: 🔄 复杂度问题:2, 📝 注释问题:1, ⚠️ 其他问题:2

主要问题:

函数 generate_single_image 的循环复杂度过高 (22)，考虑重构
函数 'INPUT_TYPES' () 过长 (86 行)，建议拆分
函数 'generate_single_image' () 极度过长 (124 行)，必须拆分
函数 'generate_single_image' () 复杂度严重过高 (22)，必须简化
代码注释率较低 (5.82%)，建议增加注释
2. /home/runner/work/comfyui-banana-pro/comfyui-banana-pro/balance_service.py (得分: 50.41)
问题分类: 🔄 复杂度问题:2, 📝 注释问题:1, ⚠️ 其他问题:1

主要问题:

函数 ensure_route 的循环复杂度过高 (21)，考虑重构
函数 'ensure_route' () 过长 (98 行)，建议拆分
函数 'ensure_route' () 复杂度严重过高 (21)，必须简化
代码注释率极低 (1.50%)，几乎没有注释