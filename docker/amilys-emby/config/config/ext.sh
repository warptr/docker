#!/bin/sh

######## 说明 2023-07-30 ########
#一个sh脚本，容器每次启动时运行
#方便自定义添加功能
#################################


echo "Emby扩展启动脚本"

#去掉下行注释可以关闭次脚本
#exit 0

########下面可以自行添加功能########

## 修改容器hosts

#echo -e "13.226.210.20     api.themoviedb.org" >> /etc/hosts
#echo -e "13.225.142.99     api4.thetvdb.com" >> /etc/hosts

## Emby-crx 美化 媒体库ID为空时不启用

## 媒体库id，用逗号分隔。进入媒体库后url里的parentId
## MediaId="21466,21463"
MediaId="3,5"

sed -i '/this.parentId/s/""\|"[0-9]\+"\|"\([0-9]\+,\)\+[0-9]\+"/"'$MediaId'"/g' /system/dashboard-ui/emby-crx/config.js

## 扩展插件: 
embyLaunchPotplayer 外部播放
ede.user 弹幕
actorPlus 未知演员隐藏
extmod='["embyLaunchPotplayer","ede.user","actorPlus"]'

extmod='[]'
sed -i '/\ extmod/s/\[.*\]/'$extmod'/g' /system/dashboard-ui/ext.js

exit 0
