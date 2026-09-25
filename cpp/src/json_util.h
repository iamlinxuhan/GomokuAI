// 手写 JSON 解析/写入。零第三方依赖（提示词要求），且**整数必须精确**。
//
// 为什么不引 nlohmann/json 之类：题目要求"不使用任何第三方库"，而本协议只有
// 一种形状的报文，一个 200 行的递归下降足够。
//
// 最容易踩的一条在写入侧：**`best_val` / `nodes` / `nps` / `depth` 必须输出
// 成 JSON 整数**，绝不能用 `%g` 或默认的 `<<` 浮点格式。写成 `1e+07` 会让
// Python 侧 `json.loads` 收到 `float`，于是 `info['best_val'] == 某个 int`
// 这类比较、以及 `analysis.py` 里按整数做分档的逻辑全部静默走偏 —— 不报错，
// 只是数字看起来"对"而类型错了。所以写入器给整数单开一个方法。

#pragma once

#include <cstdint>
#include <string>
#include <utility>
#include <vector>

namespace json {

struct Value {
    enum class Type { Null, Bool, Num, Str, Arr, Obj };

    Type type = Type::Null;
    bool boolean = false;
    double num = 0.0;
    std::string str;
    std::vector<Value> arr;
    std::vector<std::pair<std::string, Value>> obj;

    bool isNull() const { return type == Type::Null; }
    bool isNum() const { return type == Type::Num; }
    bool isArr() const { return type == Type::Arr; }
    bool isObj() const { return type == Type::Obj; }

    const Value* find(const std::string& key) const {
        if (type != Type::Obj) return nullptr;
        for (const auto& kv : obj) {
            if (kv.first == key) return &kv.second;
        }
        return nullptr;
    }
    //: 取整数；缺省或类型不符时返回 `fallback`。
    int64_t intOr(int64_t fallback) const {
        return type == Type::Num ? static_cast<int64_t>(num) : fallback;
    }
    double numOr(double fallback) const {
        return type == Type::Num ? num : fallback;
    }
    //: 缺省时返回 `fallback`；`null` 也算缺省（协议里 `"bias": null` 表示
    //: "这一档没有偏置"，与"字段没写"应当同义）。
    bool boolOr(bool fallback) const {
        return type == Type::Bool ? boolean : fallback;
    }
    std::string strOr(const std::string& fallback) const {
        return type == Type::Str ? str : fallback;
    }
};

//: 解析一段 JSON 文本。成功返回 true；失败时把原因写进 `err`。
bool parse(const std::string& text, Value* out, std::string* err);

//: 转义并追加一个 JSON 字符串（含首尾引号）。UTF-8 原样透传 —— reason 字段
//: 里带中文与 `·`，按字节透传即可，Python 侧 `json.loads` 会正确解码。
//: 声明在 `ObjWriter` **之前** —— 它要用到。
void writeString(std::string* out, const std::string& s);

//: 对象写入器。用法：`json::ObjWriter w(&buf); w.i64("x", 3); w.close();`
class ObjWriter {
public:
    explicit ObjWriter(std::string* out) : out_(out) { out_->push_back('{'); }

    void str(const char* key, const std::string& v) {
        keySep(key);
        writeString(out_, v);
    }
    void str(const char* key, const char* v) { str(key, std::string(v)); }
    //: **整数出口。** 见文件头。
    void i64(const char* key, int64_t v) {
        keySep(key);
        *out_ += std::to_string(v);
    }
    void i32(const char* key, int v) { i64(key, v); }
    //: 浮点出口。只用于 `time_ms` / 比率这类本身就是实数的字段。
    void dbl(const char* key, double v, int prec = 6);
    void boolean(const char* key, bool v) {
        keySep(key);
        *out_ += v ? "true" : "false";
    }
    void null(const char* key) {
        keySep(key);
        *out_ += "null";
    }
    void close() { out_->push_back('}'); }

private:
    void keySep(const char* key) {
        if (!first_) out_->push_back(',');
        first_ = false;
        writeString(out_, key);
        out_->push_back(':');
    }
    std::string* out_;
    bool first_ = true;
};

}  // namespace json
