#include "json_util.h"

#include <cstdio>
#include <cstdlib>

namespace json {
namespace {

class Parser {
public:
    Parser(const std::string& t, std::string* err) : s_(t), err_(err) {}

    bool run(Value* out) {
        skipWs();
        if (!parseValue(out)) return false;
        skipWs();
        if (pos_ != s_.size()) return fail("结尾有多余字符");
        return true;
    }

private:
    const std::string& s_;
    size_t pos_ = 0;
    std::string* err_;

    bool fail(const std::string& msg) {
        if (err_) {
            *err_ = msg + "（位置 " + std::to_string(pos_) + "）";
        }
        return false;
    }
    bool eof() const { return pos_ >= s_.size(); }
    char peek() const { return pos_ < s_.size() ? s_[pos_] : '\0'; }

    void skipWs() {
        while (pos_ < s_.size()) {
            const char c = s_[pos_];
            if (c == ' ' || c == '\t' || c == '\n' || c == '\r') ++pos_;
            else break;
        }
    }
    bool literal(const char* lit) {
        const size_t n = std::char_traits<char>::length(lit);
        if (s_.compare(pos_, n, lit) != 0) return false;
        pos_ += n;
        return true;
    }

    bool parseValue(Value* out) {
        skipWs();
        if (eof()) return fail("意外的结尾");
        const char c = peek();
        switch (c) {
            case '{': return parseObject(out);
            case '[': return parseArray(out);
            case '"':
                out->type = Value::Type::Str;
                return parseString(&out->str);
            case 't':
                if (!literal("true")) return fail("不是 true");
                out->type = Value::Type::Bool;
                out->boolean = true;
                return true;
            case 'f':
                if (!literal("false")) return fail("不是 false");
                out->type = Value::Type::Bool;
                out->boolean = false;
                return true;
            case 'n':
                if (!literal("null")) return fail("不是 null");
                out->type = Value::Type::Null;
                return true;
            default:
                return parseNumber(out);
        }
    }

    bool parseObject(Value* out) {
        out->type = Value::Type::Obj;
        ++pos_;                       // '{'
        skipWs();
        if (peek() == '}') { ++pos_; return true; }
        while (true) {
            skipWs();
            if (peek() != '"') return fail("对象的键必须是字符串");
            std::string key;
            if (!parseString(&key)) return false;
            skipWs();
            if (peek() != ':') return fail("键之后缺少 ':'");
            ++pos_;
            Value v;
            if (!parseValue(&v)) return false;
            out->obj.emplace_back(std::move(key), std::move(v));
            skipWs();
            if (peek() == ',') { ++pos_; continue; }
            if (peek() == '}') { ++pos_; return true; }
            return fail("对象里缺少 ',' 或 '}'");
        }
    }

    bool parseArray(Value* out) {
        out->type = Value::Type::Arr;
        ++pos_;                       // '['
        skipWs();
        if (peek() == ']') { ++pos_; return true; }
        while (true) {
            Value v;
            if (!parseValue(&v)) return false;
            out->arr.push_back(std::move(v));
            skipWs();
            if (peek() == ',') { ++pos_; continue; }
            if (peek() == ']') { ++pos_; return true; }
            return fail("数组里缺少 ',' 或 ']'");
        }
    }

    bool parseString(std::string* out) {
        ++pos_;                       // '"'
        out->clear();
        while (true) {
            if (eof()) return fail("字符串没有闭合");
            const char c = s_[pos_++];
            if (c == '"') return true;
            if (c != '\\') {
                out->push_back(c);
                continue;
            }
            if (eof()) return fail("转义符之后是结尾");
            const char e = s_[pos_++];
            switch (e) {
                case '"': out->push_back('"'); break;
                case '\\': out->push_back('\\'); break;
                case '/': out->push_back('/'); break;
                case 'b': out->push_back('\b'); break;
                case 'f': out->push_back('\f'); break;
                case 'n': out->push_back('\n'); break;
                case 'r': out->push_back('\r'); break;
                case 't': out->push_back('\t'); break;
                case 'u': {
                    // 只处理 BMP 内的码点，按 UTF-8 编码写出。协议里带 \u
                    // 转义的字段只有 type/reason 之类，代理对（emoji）不会
                    // 出现；真出现了也只会得到一个替换字符，不会崩。
                    if (pos_ + 4 > s_.size()) return fail("\\u 后面不足 4 位");
                    unsigned cp = 0;
                    for (int i = 0; i < 4; ++i) {
                        const char h = s_[pos_++];
                        cp <<= 4;
                        if (h >= '0' && h <= '9') cp |= unsigned(h - '0');
                        else if (h >= 'a' && h <= 'f') cp |= unsigned(h - 'a' + 10);
                        else if (h >= 'A' && h <= 'F') cp |= unsigned(h - 'A' + 10);
                        else return fail("\\u 后面不是十六进制");
                    }
                    if (cp < 0x80) {
                        out->push_back(char(cp));
                    } else if (cp < 0x800) {
                        out->push_back(char(0xC0 | (cp >> 6)));
                        out->push_back(char(0x80 | (cp & 0x3F)));
                    } else {
                        out->push_back(char(0xE0 | (cp >> 12)));
                        out->push_back(char(0x80 | ((cp >> 6) & 0x3F)));
                        out->push_back(char(0x80 | (cp & 0x3F)));
                    }
                    break;
                }
                default:
                    return fail("未知的转义符");
            }
        }
    }

    bool parseNumber(Value* out) {
        const char* start = s_.c_str() + pos_;
        char* endp = nullptr;
        const double v = std::strtod(start, &endp);
        if (endp == start) return fail("不是合法的数字");
        pos_ += static_cast<size_t>(endp - start);
        out->type = Value::Type::Num;
        out->num = v;
        return true;
    }
};

}  // namespace

bool parse(const std::string& text, Value* out, std::string* err) {
    Parser p(text, err);
    return p.run(out);
}

void writeString(std::string* out, const std::string& s) {
    out->push_back('"');
    for (const char c : s) {
        switch (c) {
            case '"':  *out += "\\\""; break;
            case '\\': *out += "\\\\"; break;
            case '\b': *out += "\\b"; break;
            case '\f': *out += "\\f"; break;
            case '\n': *out += "\\n"; break;
            case '\r': *out += "\\r"; break;
            case '\t': *out += "\\t"; break;
            default:
                // 控制字符必须转义；其余字节（含 UTF-8 多字节序列）原样透传。
                if (static_cast<unsigned char>(c) < 0x20) {
                    char buf[8];
                    std::snprintf(buf, sizeof(buf), "\\u%04x",
                                  static_cast<unsigned char>(c));
                    *out += buf;
                } else {
                    out->push_back(c);
                }
        }
    }
    out->push_back('"');
}

void ObjWriter::dbl(const char* key, double v, int prec) {
    keySep(key);
    char buf[64];
    std::snprintf(buf, sizeof(buf), "%.*f", prec, v);
    *out_ += buf;
}

}  // namespace json
