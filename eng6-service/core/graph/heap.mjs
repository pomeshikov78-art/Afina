/**
 * core/graph/heap.mjs — бинарная min-куча на типизированных массивах.
 *
 * Нужна для Дейкстры на графах до 10^5 узлов: обычный Array<{k,v}> даёт
 * 10-кратный рост аллокаций и GC-паузы. Здесь ключи — Float64Array,
 * значения — Int32Array, рост — геометрический (×2).
 *
 * Сложность: push O(log n), pop O(log n). Память: 12 байт на элемент.
 */
export class MinHeap {
  constructor(capacity = 1024) {
    this.cap = Math.max(16, capacity | 0);
    this.k = new Float64Array(this.cap);
    this.v = new Int32Array(this.cap);
    this.size = 0;
  }

  clear() {
    this.size = 0;
    return this;
  }

  get empty() {
    return this.size === 0;
  }

  _grow() {
    const cap = this.cap * 2;
    const k = new Float64Array(cap);
    const v = new Int32Array(cap);
    // ВНИМАНИЕ: куча 1-индексная, заняты ячейки 1..size — копировать нужно
    // size+1 элементов. Ошибка «subarray(0, size)» теряла последний элемент
    // ровно в момент роста и портила результат Дейкстры (найдено unit-тестом
    // tests/unit/graph.test.mjs → «MinHeap: порядок извлечения»).
    k.set(this.k.subarray(0, this.size + 1));
    v.set(this.v.subarray(0, this.size + 1));
    this.k = k;
    this.v = v;
    this.cap = cap;
  }

  push(key, value) {
    if (this.size + 1 >= this.cap) this._grow();
    const k = this.k;
    const v = this.v;
    let i = ++this.size;
    while (i > 1) {
      const p = i >> 1;
      if (k[p] <= key) break;
      k[i] = k[p];
      v[i] = v[p];
      i = p;
    }
    k[i] = key;
    v[i] = value;
  }

  /** @returns {[number, number]} [key, value] */
  pop() {
    const k = this.k;
    const v = this.v;
    const topK = k[1];
    const topV = v[1];
    const last = this.size--;
    if (last > 1) {
      const key = k[last];
      const val = v[last];
      let i = 1;
      for (;;) {
        let c = i << 1;
        if (c >= last) break;
        if (c + 1 < last && k[c + 1] < k[c]) c++;
        if (k[c] >= key) break;
        k[i] = k[c];
        v[i] = v[c];
        i = c;
      }
      k[i] = key;
      v[i] = val;
    }
    return [topK, topV];
  }
}

/**
 * Очередь с приоритетом по целочисленным ключам (корзины) — для Дейкстры,
 * где вес квантован (задержка в мкс). O(1) push/pop, память O(maxKey).
 * В планировщике используется как ускорение на плотных графах малого диаметра.
 */
export class BucketQueue {
  constructor(maxKey, n) {
    this.maxKey = maxKey;
    this.head = new Int32Array(maxKey + 2).fill(-1);
    this.next = new Int32Array(n).fill(-1);
    this.min = 0;
    this.count = 0;
  }

  push(key, node) {
    const k = key > this.maxKey ? this.maxKey + 1 : key;
    this.next[node] = this.head[k];
    this.head[k] = node;
    if (k < this.min) this.min = k;
    this.count++;
  }

  pop() {
    while (this.min <= this.maxKey + 1 && this.head[this.min] === -1) this.min++;
    if (this.min > this.maxKey + 1) return -1;
    const node = this.head[this.min];
    this.head[this.min] = this.next[node];
    this.count--;
    return node;
  }

  get empty() {
    return this.count === 0;
  }
}
