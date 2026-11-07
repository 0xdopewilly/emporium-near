"""Realistic job inputs per service id, cycled by the stress bench."""

CODE = [
    "def average(xs):\n    return sum(xs) / len(xs)",
    "def is_even(n):\n    return n % 2 == 1",
    "async function getUser(id) {\n  const r = fetch(`/api/users/${id}`);\n  return r.json();\n}",
    "def read_config(path):\n    f = open(path)\n    return json.load(f)",
    "for i in range(len(items)):\n    if items[i] == target:\n        del items[i]",
    "SELECT * FROM users WHERE email = '\" + email + \"';",
    "function sum(arr) { let t = 0; for (let i = 0; i <= arr.length; i++) t += arr[i]; return t; }",
    "def fib(n):\n    if n <= 1: return n\n    return fib(n-1) + fib(n-2)\nprint(fib(90))",
    "import requests\ndef fetch(url):\n    return requests.get(url, verify=False).text",
    "def transfer(a, b, amt):\n    a.balance -= amt\n    b.balance += amt",
    "const cache = {};\nfunction memo(fn) { return (x) => cache[x] ??= fn(x); }",
    "def parse_age(s):\n    return int(s) if s.isdigit() else -1",
    "user_input = input()\neval(user_input)",
    "def last(xs):\n    return xs[len(xs)]",
    "pub fn div(a: i32, b: i32) -> i32 { a / b }",
]

LAUNCH = [
    "A Hermes plugin that lets your AI agent sell its skills for USDC, paid only when the work is delivered.",
    "A Lagos food delivery app that guarantees your jollof arrives hot in 30 minutes or it's free.",
    "An open-source tool that turns any spreadsheet into a live API in one click.",
    "A savings app that rounds up every card payment and invests the change in treasury bills.",
    "A browser extension that summarises any YouTube video into five bullet points.",
    "A co-working space in Yaba with 24/7 power, fibre internet and free coffee.",
    "A budgeting assistant that reads your bank SMS alerts and tracks spending automatically.",
    "A language-learning app that teaches Yoruba through Afrobeats lyrics.",
    "A marketplace where farmers sell produce directly to restaurants, no middlemen.",
    "An AI tutor that helps secondary-school students prepare for WAEC maths.",
]

TRANSLATE = [
    "Good morning! Your order has shipped and will arrive on Friday.",
    "Thank you for your payment. Your receipt is attached.",
    "We just launched! Tell your friends and get 10% off this week.",
    "The meeting has moved to 3pm tomorrow. Please confirm you can attend.",
    "Your account has been verified. You can now send and receive money.",
    "Happy birthday! Wishing you joy, health and many more years.",
    "Please keep your password private. We will never ask for it by phone.",
    "The store opens at 9am and closes at 7pm, Monday to Saturday.",
    "Congratulations on your new job! We are proud of you.",
    "Delivery is free for orders above five thousand naira.",
    "Your appointment with the doctor is confirmed for Monday morning.",
    "Rain is expected this afternoon, so carry an umbrella.",
]

QUESTIONS = [
    "What changed in the x402 protocol v2?",
    "What is EIP-3009 and which tokens support it?",
    "How do Base Sepolia faucets for USDC work?",
    "What is the Hermes Agent from Nous Research?",
    "What are the main differences between Base and Optimism?",
    "How does Circle's USDC handle gasless transfers?",
]

INPUTS = {
    "code-review": CODE,
    "launch-post": LAUNCH,
    "translate-yoruba": TRANSLATE,
    "translate-pidgin": TRANSLATE,
    "web-brief": QUESTIONS,
}
