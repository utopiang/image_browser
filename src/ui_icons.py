"""轻量线性图标：统一笔画、透明背景，高 DPI 下按需绘制。"""
from PIL import Image, ImageDraw, ImageTk


class IconFactory:
    def __init__(self, master):
        self.master = master
        self.cache = {}

    def get(self, name, size=22, color="#526176"):
        key = name, size, color
        if key in self.cache:
            return self.cache[key]
        scale = 4
        image = Image.new("RGBA", (size*scale, size*scale))
        draw = ImageDraw.Draw(image)
        factor = size*scale/24
        def points(values):
            return [(round(x*factor), round(y*factor)) for x,y in values]
        def line(values, width=1.7, fill=color):
            draw.line(points(values), fill=fill, width=max(1,round(width*factor)), joint="curve")
        def box(rect, width=1.7, fill=None):
            draw.rounded_rectangle(tuple(round(v*factor) for v in rect), radius=round(1.5*factor), outline=color, width=max(1,round(width*factor)), fill=fill)
        def circle(rect, fill=None):
            draw.ellipse(tuple(round(v*factor) for v in rect), outline=color, width=max(1,round(1.7*factor)), fill=fill)
        if name == "folder":
            line([(3,7),(3,5),(9,5),(11,8),(21,8),(21,20),(3,20),(3,7),(21,7)])
        elif name == "save":
            line([(4,3),(18,3),(21,6),(21,21),(3,21),(3,3),(4,3)])
            box((7,3,16,10)); box((7,14,17,21))
        elif name == "undo":
            line([(9,5),(4,10),(9,15)]); line([(5,10),(14,10),(18,12),(19,16),(17,20)])
        elif name == "select":
            line([(5,3),(5,20),(10,15),(14,22),(17,20),(13,13),(20,12),(5,3)])
        elif name == "hand":
            line([(5,13),(5,9),(7,9),(8,12),(8,4),(10,4),(10,11),(11,3),(13,3),(13,11),(14,5),(16,5),(16,12),(17,8),(19,8),(19,16),(16,21),(10,21),(5,15),(5,13)])
        elif name == "rectangle":
            box((4,6,20,19))
            for x,y in [(4,6),(20,6),(20,19),(4,19)]:
                box((x-1.5,y-1.5,x+1.5,y+1.5),width=1,fill="#f3f5f9")
        elif name == "obb":
            line([(3,10),(15,4),(21,15),(9,21),(3,10)])
            line([(15,4),(17,1),(21,3)])
            for x,y in [(3,10),(15,4),(21,15),(9,21)]:
                circle((x-1.3,y-1.3,x+1.3,y+1.3),fill=color)
        elif name == "polygon":
            vertices=[(4,7),(15,3),(21,12),(16,21),(4,17)]
            line(vertices+[vertices[0]])
            for x,y in vertices: circle((x-1.5,y-1.5,x+1.5,y+1.5),fill=color)
        elif name == "fit":
            for vertices in [[(3,9),(3,3),(9,3)],[(15,3),(21,3),(21,9)],[(21,15),(21,21),(15,21)],[(9,21),(3,21),(3,15)]]:
                line(vertices)
            box((7,7,17,17),width=1.4)
        elif name == "eye":
            line([(2,12),(6,7),(12,5),(18,7),(22,12),(18,17),(12,19),(6,17),(2,12)])
            circle((9,9,15,15))
        elif name == "accept":
            circle((2,2,22,22), fill=color)
            line([(6.5,12),(10.5,16),(18,8.5)], width=2.6, fill="#ffffff")
        elif name == "trash":
            line([(3,6),(21,6)]); line([(8,6),(8,3),(16,3),(16,6)])
            line([(5,6),(6,21),(18,21),(19,6)]); line([(10,10),(10,17)]); line([(14,10),(14,17)])
        elif name in {"import", "export"}:
            box((3,12,21,21)); line([(12,3),(12,16)])
            line([(7,11),(12,16),(17,11)] if name=="import" else [(7,8),(12,3),(17,8)])
        elif name in {"previous", "next", "chevron"}:
            line([(15,5),(8,12),(15,19)] if name=="previous" else [(9,5),(16,12),(9,19)])
        elif name == "search":
            circle((3,3,16,16)); line([(15,15),(21,21)],width=2)
        elif name == "plus":
            line([(12,4),(12,20)]); line([(4,12),(20,12)])
        elif name == "close":
            line([(6,6),(18,18)]); line([(18,6),(6,18)])
        elif name == "video":
            box((3,5,21,20)); line([(10,9),(16,12.5),(10,16),(10,9)])
        elif name == "duplicates":
            box((3,3,16,17)); box((8,8,21,22))
        elif name == "workspace":
            box((2,4,22,21)); line([(7,4),(7,21)]); line([(17,4),(17,21)]); line([(9,17),(12,11),(15,16)])
        elif name == "tag":
            line([(3,3),(13,3),(22,12),(12,22),(3,13),(3,3)]); circle((6,6,9,9))
        elif name == "settings":
            for y,x in [(6,8),(12,16),(18,10)]:
                line([(3,y),(21,y)]); circle((x-2,y-2,x+2,y+2),fill="#f3f5f9")
        else:
            box((4,4,20,20))
        result = ImageTk.PhotoImage(image.resize((size,size),Image.Resampling.LANCZOS),master=self.master)
        self.cache[key] = result
        return result
