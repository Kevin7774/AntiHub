import http from 'k6/http';
import { check, sleep } from 'k6';

export const options = {
  vus: Number(__ENV.VUS || 100),
  duration: __ENV.DURATION || '1m',
  thresholds: {
    http_req_duration: ['p(99)<800'],
    http_req_failed: ['rate<0.10'],
  },
};

const BASE_URL = __ENV.BASE_URL || 'http://127.0.0.1:8010';
const LOGIN_USER = __ENV.LOGIN_USER || 'Zed';
const LOGIN_PASSWORD = __ENV.LOGIN_PASSWORD || 'AFKzzd123';
const AUTH_TOKEN = (__ENV.AUTH_TOKEN || '').trim();

function loginAndGetToken() {
  if (AUTH_TOKEN) {
    return AUTH_TOKEN;
  }

  const response = http.post(
    `${BASE_URL}/auth/login`,
    JSON.stringify({ username: LOGIN_USER, password: LOGIN_PASSWORD }),
    {
      headers: { 'Content-Type': 'application/json' },
      timeout: '10s',
    }
  );

  check(response, {
    'login status is 200': (r) => r.status === 200,
  });

  const json = response.json();
  return String((json && json.access_token) || '');
}

export function setup() {
  const token = loginAndGetToken();
  if (!token) {
    throw new Error('No auth token. Set AUTH_TOKEN or valid LOGIN_USER/LOGIN_PASSWORD.');
  }
  return { token };
}

export default function (data) {
  const payload = {
    query: __ENV.QUERY || 'CRM SaaS with payment and audit logs',
    mode: __ENV.MODE || 'quick',
    limit: String(__ENV.LIMIT || 8),
  };

  const response = http.post(`${BASE_URL}/recommendations`, payload, {
    headers: {
      Authorization: `Bearer ${data.token}`,
      'Content-Type': 'application/x-www-form-urlencoded',
    },
    timeout: __ENV.REQUEST_TIMEOUT || '20s',
  });

  check(response, {
    'recommendations success or throttled': (r) => r.status === 200 || r.status === 429,
  });

  sleep(Number(__ENV.SLEEP_SEC || 0.2));
}
