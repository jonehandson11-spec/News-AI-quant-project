const DATABASE = "news-price-query";
const STORE = "price-files";
const KEY = "selected-csv";

function requestFromStore(mode, makeRequest) {
  return new Promise((resolve, reject) => {
    if (typeof indexedDB === "undefined") {
      reject(new Error("此浏览器不支持本机价格数据库。"));
      return;
    }
    const opened = indexedDB.open(DATABASE, 1);
    opened.onupgradeneeded = () => opened.result.createObjectStore(STORE);
    opened.onerror = () => reject(opened.error);
    opened.onsuccess = () => {
      const database = opened.result;
      const transaction = database.transaction(STORE, mode);
      const request = makeRequest(transaction.objectStore(STORE));
      let value;
      request.onsuccess = () => { value = request.result; };
      transaction.oncomplete = () => { database.close(); resolve(value); };
      transaction.onerror = () => { database.close(); reject(transaction.error); };
      transaction.onabort = () => { database.close(); reject(transaction.error || new Error("本机价格数据库操作中断。")); };
    };
  });
}

export function loadSavedPriceCsv() {
  return requestFromStore("readonly", (store) => store.get(KEY));
}

export function savePriceCsv(name, text) {
  return requestFromStore("readwrite", (store) => store.put({ name, text }, KEY));
}

export function deleteSavedPriceCsv() {
  return requestFromStore("readwrite", (store) => store.delete(KEY));
}
