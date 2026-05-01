const fs = require("fs");
const path = require("path");

class JsonParser {
  constructor(text, filePath) {
    this.text = text;
    this.filePath = filePath;
    this.index = 0;
    this.duplicateKeys = [];
  }

  parse() {
    const value = this.parseValue([]);
    this.skipWhitespace();
    if (this.index !== this.text.length) {
      this.throwError("Unexpected trailing content");
    }
    return { value, duplicateKeys: this.duplicateKeys };
  }

  parseValue(pathStack) {
    this.skipWhitespace();
    const char = this.peek();
    if (char === "{") {
      return this.parseObject(pathStack);
    }
    if (char === "[") {
      return this.parseArray(pathStack);
    }
    if (char === "\"") {
      return this.parseString();
    }
    if (char === "t") {
      return this.parseLiteral("true", true);
    }
    if (char === "f") {
      return this.parseLiteral("false", false);
    }
    if (char === "n") {
      return this.parseLiteral("null", null);
    }
    if (char === "-" || this.isDigit(char)) {
      return this.parseNumber();
    }
    this.throwError("Unexpected token");
  }

  parseObject(pathStack) {
    this.consume("{");
    this.skipWhitespace();
    const result = {};
    const keySet = new Set();

    if (this.peek() === "}") {
      this.index += 1;
      return result;
    }

    while (true) {
      this.skipWhitespace();
      if (this.peek() !== "\"") {
        this.throwError("Expected string key");
      }
      const key = this.parseString();
      if (keySet.has(key)) {
        this.duplicateKeys.push({ path: [...pathStack, key] });
      } else {
        keySet.add(key);
      }

      this.skipWhitespace();
      this.consume(":");
      const value = this.parseValue([...pathStack, key]);
      result[key] = value;
      this.skipWhitespace();

      const next = this.peek();
      if (next === "}") {
        this.index += 1;
        break;
      }
      this.consume(",");
    }

    return result;
  }

  parseArray(pathStack) {
    this.consume("[");
    this.skipWhitespace();
    const result = [];
    if (this.peek() === "]") {
      this.index += 1;
      return result;
    }

    let index = 0;
    while (true) {
      const value = this.parseValue([...pathStack, index]);
      result.push(value);
      index += 1;
      this.skipWhitespace();

      const next = this.peek();
      if (next === "]") {
        this.index += 1;
        break;
      }
      this.consume(",");
    }

    return result;
  }

  parseString() {
    this.consume("\"");
    let result = "";

    while (this.index < this.text.length) {
      const char = this.text[this.index];
      if (char === "\"") {
        this.index += 1;
        return result;
      }
      if (char === "\\") {
        this.index += 1;
        const escaped = this.text[this.index];
        if (escaped === undefined) {
          this.throwError("Unterminated escape sequence");
        }
        if (escaped === "\"" || escaped === "\\" || escaped === "/") {
          result += escaped;
        } else if (escaped === "b") {
          result += "\b";
        } else if (escaped === "f") {
          result += "\f";
        } else if (escaped === "n") {
          result += "\n";
        } else if (escaped === "r") {
          result += "\r";
        } else if (escaped === "t") {
          result += "\t";
        } else if (escaped === "u") {
          const hex = this.text.slice(this.index + 1, this.index + 5);
          if (!/^[0-9a-fA-F]{4}$/.test(hex)) {
            this.throwError("Invalid unicode escape");
          }
          result += String.fromCharCode(parseInt(hex, 16));
          this.index += 4;
        } else {
          this.throwError("Invalid escape sequence");
        }
        this.index += 1;
        continue;
      }
      if (char < " ") {
        this.throwError("Unescaped control character in string");
      }
      result += char;
      this.index += 1;
    }

    this.throwError("Unterminated string");
  }

  parseNumber() {
    const remaining = this.text.slice(this.index);
    const match = /^-?(0|[1-9]\d*)(\.\d+)?([eE][+-]?\d+)?/.exec(remaining);
    if (!match) {
      this.throwError("Invalid number");
    }
    this.index += match[0].length;
    return Number(match[0]);
  }

  parseLiteral(literal, value) {
    if (this.text.slice(this.index, this.index + literal.length) !== literal) {
      this.throwError(`Expected ${literal}`);
    }
    this.index += literal.length;
    return value;
  }

  skipWhitespace() {
    while (this.index < this.text.length) {
      const char = this.text[this.index];
      if (char === " " || char === "\n" || char === "\r" || char === "\t") {
        this.index += 1;
        continue;
      }
      break;
    }
  }

  peek() {
    return this.text[this.index];
  }

  consume(expected) {
    if (this.text[this.index] !== expected) {
      this.throwError(`Expected '${expected}'`);
    }
    this.index += 1;
  }

  isDigit(char) {
    return char >= "0" && char <= "9";
  }

  throwError(message) {
    const { line, column } = this.getLineColumn(this.index);
    const error = new Error(`${message} at ${this.filePath}:${line}:${column}`);
    throw error;
  }

  getLineColumn(position) {
    const slice = this.text.slice(0, position);
    const lines = slice.split("\n");
    return { line: lines.length, column: lines[lines.length - 1].length + 1 };
  }
}

function formatPath(pathParts) {
  return pathParts.reduce((acc, part) => {
    if (typeof part === "number") {
      return `${acc}[${part}]`;
    }
    return acc ? `${acc}.${part}` : part;
  }, "");
}

function validateTranslationFiles() {
  const translationsDir = path.join(__dirname, "..", "translations");
  const files = fs
    .readdirSync(translationsDir)
    .filter((file) => file.endsWith(".json"))
    .sort();

  const errors = [];

  for (const file of files) {
    const filePath = path.join(translationsDir, file);
    const content = fs.readFileSync(filePath, "utf8");

    try {
      const parser = new JsonParser(content, filePath);
      const { duplicateKeys } = parser.parse();
      if (duplicateKeys.length > 0) {
        duplicateKeys.forEach((entry) => {
          errors.push(
            `Duplicate key '${formatPath(entry.path)}' in ${filePath}`
          );
        });
      }
    } catch (error) {
      errors.push(error.message);
    }
  }

  if (errors.length > 0) {
    console.error("Translation validation failed:\n");
    errors.forEach((error) => console.error(`- ${error}`));
    process.exit(1);
  }

  console.log("Translation validation passed.");
}

validateTranslationFiles();
