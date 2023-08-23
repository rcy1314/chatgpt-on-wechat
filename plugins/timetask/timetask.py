# encoding:utf-8
import plugins
from bridge.context import ContextType, Context
from bridge.reply import Reply, ReplyType
from channel.chat_message import ChatMessage
import logging
from plugins import *
import logging
from plugins.timetask.TimeTaskTool import TaskManager
from plugins.timetask.config import conf, load_config
from plugins.timetask.Tool import TimeTaskModel
from lib import itchat
from lib.itchat.content import *
import re
import arrow
from plugins.timetask.Tool import ExcelTool
from bridge.bridge import Bridge
import config as RobotConfig
import requests
import io
import time
import gc
from channel import channel_factory

class TimeTaskRemindType(Enum):
    NO_Task = 1           #无任务
    Add_Success = 2       #添加任务成功
    Add_Failed = 3        #添加任务失败
    Cancel_Success = 4    #取消任务成功
    Cancel_Failed = 5     #取消任务失败
    TaskList_Success = 6  #查看任务列表成功
    TaskList_Failed = 7   #查看任务列表失败

@plugins.register(
    name="timetask",
    desire_priority=500,
    hidden=True,
    desc="定时任务系统，可定时处理事件",
    version="2.3",
    author="haikerwang",
)
    
class timetask(Plugin):
    
    def __init__(self):
        super().__init__()
        self.handlers[Event.ON_HANDLE_CONTEXT] = self.on_handle_context
        print("[timetask] inited")
        load_config()
        self.conf = conf()
        self.taskManager = TaskManager(self.runTimeTask)
        
    def on_handle_context(self, e_context: EventContext):
        if e_context["context"].type not in [
            ContextType.TEXT,
        ]:
            return
        
        #查询内容
        query = e_context["context"].content
        logging.info("定时任务的输入信息为:{}".format(query))
        #指令前缀
        command_prefix = self.conf.get("command_prefix", "$time")
        
        #需要的格式：$time 时间 事件
        if query.startswith(command_prefix) :
            #处理任务
            print("[timetask] 捕获到定时任务:{}".format(query))
            #移除指令
            #示例：$time 明天 十点十分 提醒我健身
            content = query.replace(f"{command_prefix} ", "")
            content = content.replace(command_prefix, "")
            self.deal_timeTask(content, e_context)

    #处理时间任务
    def deal_timeTask(self, content, e_context: EventContext):
        
        if content.startswith("取消任务"):
            self.cancel_timeTask(content, e_context)
            
        elif content.startswith("任务列表"):
            self.get_timeTaskList(content, e_context)
            
        else:
            self.add_timeTask(content, e_context)
        
    #取消任务
    def cancel_timeTask(self, content, e_context: EventContext):
        #分割
        wordsArray = content.split(" ")
        #任务Id
        taskId = wordsArray[1]
        isExist, taskModel = ExcelTool().write_columnValue_withTaskId_toExcel(taskId, 2, "0")
        taskContent = "未知"
        if taskModel:
            taskContent = f"{taskModel.circleTimeStr} {taskModel.timeStr} {taskModel.eventStr}"
            if taskModel.isCron_time():
                taskContent = f"{taskModel.circleTimeStr} {taskModel.eventStr}"
        #回消息
        reply_text = ""
        tempStr = ""
        #文案
        if isExist:
            tempStr = self.get_default_remind(TimeTaskRemindType.Cancel_Success)
            reply_text = "⏰定时任务，取消成功~\n" + "【任务ID】：" + taskId + "\n" + "【任务详情】：" + taskContent
        else:
            tempStr = self.get_default_remind(TimeTaskRemindType.Cancel_Failed)
            reply_text = "⏰定时任务，取消失败😭，未找到任务ID，请核查\n" + "【任务ID】：" + taskId
        
        #拼接提示
        reply_text = reply_text + tempStr
        #回复
        self.replay_use_default(reply_text, e_context)  
        
        #刷新内存列表
        self.taskManager.refreshDataFromExcel()
        
        
    #获取任务列表
    def get_timeTaskList(self, content, e_context: EventContext):
        
        #任务列表
        taskArray = ExcelTool().readExcel()
        tempArray = []
        for item in taskArray:
            model = TimeTaskModel(item, None, False)
            if model.enable and model.taskId and len(model.taskId) > 0:
                isToday = model.is_today()
                is_now, _ = model.is_nowTime()
                isNowOrFeatureTime = model.is_featureTime() or is_now
                isCircleFeatureDay = model.is_featureDay()
                if (isToday and isNowOrFeatureTime) or isCircleFeatureDay:
                    tempArray.append(model)
        
        #回消息
        reply_text = ""
        tempStr = ""
        if len(tempArray) <= 0:
            tempStr = self.get_default_remind(TimeTaskRemindType.NO_Task)
            reply_text = "⏰当前无待执行的任务列表"
        else:
            tempStr = self.get_default_remind(TimeTaskRemindType.TaskList_Success)
            reply_text = "⏰定时任务列表如下：\n\n"
            #根据时间排序
            sorted_times = sorted(tempArray, key=lambda x: self.custom_sort(x.timeStr))
            for model in sorted_times:
                taskModel : TimeTaskModel = model
                tempTimeStr = f"{taskModel.circleTimeStr} {taskModel.timeStr}"
                if taskModel.isCron_time():
                    tempTimeStr = f"{taskModel.circleTimeStr}"
                reply_text = reply_text + f"【{taskModel.taskId}】@{taskModel.fromUser}: {tempTimeStr} {taskModel.eventStr}\n"   
            #移除最后一个换行    
            reply_text = reply_text.rstrip('\n')
            
        #拼接提示
        reply_text = reply_text + tempStr
        
        #回复
        self.replay_use_default(reply_text, e_context)    
        
          
    #添加任务
    def add_timeTask(self, content, e_context: EventContext):
        #失败时，默认提示
        defaultErrorMsg = "⏰定时任务指令格式异常😭，请核查！" + self.get_default_remind(TimeTaskRemindType.Add_Failed)
        
        #周期、时间、事件
        circleStr, timeStr, eventStr = self.get_timeInfo(content)
              
        #容错
        if len(circleStr) <= 0 or len(timeStr) <= 0 or len(eventStr) <= 0 :
            self.replay_use_default(defaultErrorMsg, e_context)
            return
        
        #0：ID - 唯一ID (自动生成，无需填写) 
        #1：是否可用 - 0/1，0=不可用，1=可用
        #2：时间信息 - 格式为：HH:mm:ss
        #3：轮询信息 - 格式为：每天、每周X、YYYY-MM-DD
        #4：消息内容 - 消息内容
        msg: ChatMessage = e_context["context"]["msg"]
        taskInfo = ("",
                    "1", 
                    timeStr, 
                    circleStr, 
                    eventStr, 
                    msg)
        #model
        taskModel = TimeTaskModel(taskInfo, msg, True)
        if not taskModel.isCron_time():
            #时间转换错误
            if len(taskModel.timeStr) <= 0 or len(taskModel.circleTimeStr) <= 0:
                self.replay_use_default(defaultErrorMsg, e_context)
                return
        else:
            #cron表达式格式错误
            if not taskModel.isValid_Cron_time():
               self.replay_use_default(defaultErrorMsg, e_context)
               return
        
        #task入库
        taskId = self.taskManager.addTask(taskModel)
        #回消息
        reply_text = ""
        tempStr = ""
        if len(taskId) > 0:
            tempStr = self.get_default_remind(TimeTaskRemindType.Add_Success)
            taskStr = ""
            if taskModel.isCron_time():
                taskStr = f"{circleStr} {taskModel.eventStr}"
            else:
                taskStr = f"{circleStr} {timeStr} {taskModel.eventStr}"
            reply_text = f"恭喜你，⏰定时任务已创建成功🎉~\n【任务ID】：{taskId}\n【任务详情】：{taskStr}"
        else:
            tempStr = self.get_default_remind(TimeTaskRemindType.Add_Failed)
            reply_text = f"sorry，⏰定时任务创建失败😭"
            
        #拼接提示
        reply_text = reply_text + tempStr
            
        #回复
        self.replay_use_default(reply_text, e_context)
        
    #获取时间信息
    def get_timeInfo(self, content):
        #周期
        circleStr = ""
        #时间
        timeStr = ""
        #事件
        eventStr = ""
            
        #时间格式判定
        if content.startswith("cron[") or content.startswith("Cron[") :
            #cron表达式； 格式示例："cron[0,30 14 * 3 3] 吃饭"
            # 找到第一个 "]"
            cron_end_index = content.find("]")
            #找到了
            if cron_end_index != -1:
                # 分割字符串为 A 和 B
                corn_string = content[:cron_end_index+1]
                eventStr :str = content[cron_end_index + 1:]
                eventStr = eventStr.strip()
                circleStr = corn_string
                timeStr = corn_string
            else:
                print("cron表达式 格式异常！")
                
        else:  
            #分割
            wordsArray = content.split(" ")
            if len(wordsArray) <= 2:
                logging.info("指令格式异常，请核查")
            else:
                #指令解析
                #周期
                circleStr = wordsArray[0]
                #时间
                timeStr = wordsArray[1]
                #事件
                eventStr = ' '.join(map(str, wordsArray[2:])).strip()
        
        return circleStr, timeStr, eventStr
            
    
    #使用默认的回复
    def replay_use_default(self, reply_message, e_context: EventContext):
        #回复内容
        reply = Reply()
        reply.type = ReplyType.TEXT
        reply.content = reply_message
        e_context["reply"] = reply
        e_context.action = EventAction.BREAK_PASS  # 事件结束，并跳过处理context的默认逻辑
        
    #使用自定义回复
    def replay_use_custom(self, model: TimeTaskModel, reply_text: str, replyType: ReplyType, context :Context, retry_cnt=0):
                
        try:    
            reply = Reply()
            reply.type = replyType
            reply.content = reply_text
            channel_name = RobotConfig.conf().get("channel_type", "wx")
            channel = channel_factory.create_channel(channel_name)
            channel.send(reply, context)
            
            #释放
            channel = None
            gc.collect()    
                
        except Exception as e:
            if retry_cnt < 2:
                time.sleep(3 + 3 * retry_cnt)
                self.replay_use_custom(model, reply_text, replyType, context,retry_cnt + 1)
            
        
    #执行定时task
    def runTimeTask(self, model: TimeTaskModel):
        
        print("触发了定时任务：{} , 任务详情：{}".format(model.taskId, model.eventStr))
        
        #去除多余字符串
        orgin_string = model.originMsg.replace("ChatMessage:", "")
        # 使用正则表达式匹配键值对
        pattern = r'(\w+)\s*=\s*([^,]+)'
        matches = re.findall(pattern, orgin_string)
        # 创建字典
        content_dict = {match[0]: match[1] for match in matches}
        #替换源消息中的指令
        content_dict["content"] = model.eventStr
        #添加必要key
        content_dict["receiver"] = model.other_user_id
        content_dict["session_id"] = model.other_user_id
        content_dict["isgroup"] = model.isGroup
        msg : ChatMessage = ChatMessage(content_dict)
        content_dict["msg"] = msg
        context = Context(ContextType.TEXT, model.eventStr, content_dict)
                
        #查看配置中是否开启拓展功能
        is_open_extension_function = self.conf.get("is_open_extension_function", True)
        #需要拓展功能
        if is_open_extension_function:
            #事件字符串
            event_content = model.eventStr
            #支持的功能
            funcArray = self.conf.get("extension_function", [])
            #是否是GPT消息
            isGPT = False
            for item in funcArray:
              key_word = item["key_word"]
              func_command_prefix = item["func_command_prefix"]
              #匹配到了拓展功能
              isFindExFuc = False
              if event_content.startswith(key_word):
                index = event_content.find(key_word)
                insertStr = func_command_prefix + key_word 
                if func_command_prefix == "GPT":
                      isGPT = True
                      insertStr = ""
                #内容体      
                event_content = event_content[:index] + insertStr + event_content[index+len(key_word):]
                isFindExFuc = True
                break
            
            #找到了拓展功能
            e_context = None
            if isFindExFuc:
                #替换源消息中的指令
                content_dict["content"] = event_content
                msg : ChatMessage = ChatMessage(content_dict)
                content_dict["msg"] = msg
                context = Context(ContextType.TEXT, event_content, content_dict)
                
                #GPT处理
                if isGPT:
                    content = context.content.strip()
                    imgPrefix = RobotConfig.conf().get("image_create_prefix")
                    img_match_prefix = self.check_prefix(content, imgPrefix)
                    if img_match_prefix:
                        content = content.replace(img_match_prefix, "", 1)
                        context.type = ContextType.IMAGE_CREATE
                    
                    #获取回复信息
                    replay :Reply = Bridge().fetch_reply_content(content, context)
                    self.replay_use_custom(model,replay.content,replay.type, context)
                    return
                
                try:
                    #检测插件是否会消费该消息
                    e_context = PluginManager().emit_event(
                        EventContext(
                            Event.ON_HANDLE_CONTEXT,
                            {"channel": self, "context": context, "reply": Reply()},
                        )
                    )
                except  Exception as e:
                    print(f"路由插件异常！将使用原消息回复。错误信息：{e}")
            
        #回复处理
        reply_text = ""
        replyType = None
        #插件消息
        if e_context:
            reply = e_context["reply"]
            if reply and reply.type: 
                reply_text = reply.content
                replyType = reply.type
            
        #原消息
        if reply_text is None or len(reply_text) <= 0:
            reply_text = f"⏰叮铃铃，定时任务时间已到啦~\n【任务ID】：{model.taskId}\n【任务详情】：{model.eventStr}"
            replyType = ReplyType.TEXT
                
        #消息回复
        self.replay_use_custom(model, reply_text, replyType, context)


    #检查前缀是否匹配
    def check_prefix(self, content, prefix_list):
        if not prefix_list:
            return None
        for prefix in prefix_list:
            if content.startswith(prefix):
                return prefix
        return None

    # 自定义排序函数，将字符串解析为 arrow 对象，并按时间进行排序
    def custom_sort(self, time):
        #cron - 排列最后
        if time.startswith("cron"):
            return arrow.get("23:59:59", "HH:mm:ss")
        
        #普通时间
        return arrow.get(time, "HH:mm:ss")
    
    # 默认的提示
    def get_default_remind(self, currentType: TimeTaskRemindType):
        #head
        head = "\n\n【温馨提示】\n"
        addTask = "👉添加任务：$time 明天 十点十分 提醒我健身" + "\n" + "👉cron任务：$time cron[0 * * * *] 准点报时" + "\n"
        addGPTTask = "👉GPT任务：$time 明天 十点十分 GPT 夸夸我" + "\n"
        cancelTask = "👉取消任务：$time 取消任务 任务ID" + "\n"
        taskList = "👉任务列表：$time 任务列表" + "\n"
        more = "👉更多功能：#help timetask"
        
        # NO_Task = 1           #无任务
        # Add_Success = 2       #添加任务成功
        # Add_Failed = 3        #添加任务失败
        # Cancel_Success = 4    #取消任务成功
        # Cancel_Failed = 5     #取消任务失败
        # TaskList_Success = 6  #查看任务列表成功
        # TaskList_Failed = 7   #查看任务列表失败
    
        #组装
        tempStr = head
        if currentType == TimeTaskRemindType.NO_Task:
           tempStr = tempStr + addTask + addGPTTask + cancelTask + taskList
            
        elif currentType == TimeTaskRemindType.Add_Success:
            tempStr = tempStr + cancelTask + taskList
            
        elif currentType == TimeTaskRemindType.Add_Failed:
            tempStr = tempStr + addTask + addGPTTask + cancelTask + taskList
            
        elif currentType == TimeTaskRemindType.Cancel_Success:
            tempStr = tempStr + addTask + addGPTTask + taskList 
            
        elif currentType == TimeTaskRemindType.Cancel_Failed:
            tempStr = tempStr + addTask + addGPTTask + cancelTask + taskList
            
        elif currentType == TimeTaskRemindType.TaskList_Success:
            tempStr = tempStr + addTask + addGPTTask + cancelTask
            
        elif currentType == TimeTaskRemindType.TaskList_Failed:
            tempStr = tempStr + addTask + addGPTTask + cancelTask + taskList   
                      
        else:
          tempStr = tempStr + addTask + addGPTTask + cancelTask + taskList
          
        #拼接help指令
        tempStr = tempStr + more
          
        return tempStr
    
    #help信息
    def get_help_text(self, **kwargs):
        h_str = "🎉功能一：添加定时任务\n"
        codeStr = "【指令】：$time 周期 时间 事件\n"
        circleStr = "【周期】：今天、明天、后天、每天、工作日、每周X（如：每周三）、YYYY-MM-DD的日期、cron表达式\n"
        timeStr = "【时间】：X点X分（如：十点十分）、HH:mm:ss的时间\n"
        enventStr = "【事件】：早报、点歌、搜索、GPT、文案提醒（如：提醒我健身）\n"
        exampleStr = "\n👉提醒任务示例：$time 明天 十点十分 提醒我健身\n" + "👉cron任务示例：$time cron[0 * * * *] 准点报时" + "\n"
        exampleStr0 = "👉GPT任务示例：$time 明天 十点十分 GPT 夸夸我\n\n\n"
        tempStr = h_str + codeStr + circleStr + timeStr + enventStr + exampleStr + exampleStr0
        
        h_str1 = "🎉功能二：取消定时任务\n"
        codeStr1 = "【指令】：$time 取消任务 任务ID\n"
        taskId1 = "【任务ID】：任务ID（添加任务成功时，机器人回复中有）\n"
        exampleStr1 = "\n👉示例：$time 取消任务 urwOi0he\n\n\n"
        tempStr1 = h_str1 + codeStr1 + taskId1 + exampleStr1
        
        h_str2 = "🎉功能三：获取任务列表\n"
        codeStr2 = "【指令】：$time 任务列表\n"
        exampleStr2 = "\n👉示例：$time 任务列表\n\n\n"
        tempStr2 = h_str2 + codeStr2 + exampleStr2
        
        headStr = "📌 功能介绍：添加定时任务、取消定时任务、获取任务列表。\n\n"
        help_text = headStr + tempStr + tempStr1 + tempStr2
        return help_text
