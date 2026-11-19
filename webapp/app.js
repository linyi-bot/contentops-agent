/* ContentOps 前端（Vue 3，无构建步骤）
 *
 * 为什么不用 Vite/SFC：这台机器出网受限，npm install 随时会挂；
 * 用 `vue.global.prod.js` + 一个 app.js 足够做完「选题 / 审稿 / 出图」三步，
 * 而且打开即用、改完刷新就生效 —— 演示当天少一个会坏的环节。
 */
const { createApp, ref, computed, onMounted, onUnmounted } = Vue;

const BADGE = {
  paused: ["warn", "等你操作"],
  completed: ["ok", "已完成"],
  ended: ["", "未完成"],
  error: ["bad", "出错"],
  running: ["", "运行中"],
};

const STEPS = [
  ["topics", "出选题"],
  ["draft", "写稿"],
  ["review", "人工审"],
  ["images", "出图"],
];

createApp({
  setup() {
    const runs = ref([]);
    const current = ref(null);       // { run, state, packet, step, events }
    const topic = ref("");
    const note = ref("");
    const busy = ref(false);
    const error = ref("");
    let timer = null;

    const badge = (s) => BADGE[s] || ["", s];

    const done = computed(() => {
      const st = current.value?.state || {};
      const d = new Set();
      if ((st.generated_topics || []).length) d.add("topics");
      if (st.article_content) d.add("draft");
      if (st.review_decision) d.add("review");
      if ((st.image_urls || []).length) d.add("images");
      return d;
    });

    const activeStep = computed(() => {
      const step = current.value?.step;
      if (step === "select_topic") return "topics";
      if (step === "review") return "review";
      return "";
    });

    async function api(path, opts = {}) {
      const resp = await fetch(path, {
        headers: { "Content-Type": "application/json" },
        ...opts,
      });
      if (!resp.ok) {
        let detail = resp.statusText;
        try { detail = (await resp.json()).detail || detail; } catch (e) {}
        throw new Error(detail);
      }
      return resp.json();
    }

    async function loadRuns() {
      const data = await api("/api/runs");
      runs.value = data.runs || [];
    }

    async function openRun(id) {
      current.value = await api(`/api/runs/${id}`);
      if (current.value.step === "review") note.value = "";
    }

    async function createRun() {
      const t = topic.value.trim();
      if (!t || busy.value) return;
      busy.value = true; error.value = "";
      try {
        const { run_id } = await api("/api/runs", {
          method: "POST",
          body: JSON.stringify({ topic: t }),
        });
        topic.value = "";
        await loadRuns();
        await openRun(run_id);
      } catch (e) { error.value = String(e.message || e); }
      finally { busy.value = false; }
    }

    async function selectTopic(option) {
      if (busy.value) return;
      busy.value = true; error.value = "";
      try {
        await api(`/api/runs/${current.value.run.run_id}/select`, {
          method: "POST",
          body: JSON.stringify({ selected_topic: option }),
        });
        await openRun(current.value.run.run_id);
        await loadRuns();
      } catch (e) { error.value = String(e.message || e); }
      finally { busy.value = false; }
    }

    async function review(decision) {
      if (busy.value) return;
      busy.value = true; error.value = "";
      try {
        await api(`/api/runs/${current.value.run.run_id}/review`, {
          method: "POST",
          body: JSON.stringify({ decision, note: note.value }),
        });
        note.value = "";
        await openRun(current.value.run.run_id);
        await loadRuns();
      } catch (e) { error.value = String(e.message || e); }
      finally { busy.value = false; }
    }

    /* 每 4 秒轻量刷新列表；有选中运行就一起刷新（长任务跑完能自己变） */
    function tick() {
      if (busy.value) return;
      loadRuns().catch(() => {});
      if (current.value && current.value.step) {
        openRun(current.value.run.run_id).catch(() => {});
      }
    }

    onMounted(async () => {
      try { await loadRuns(); } catch (e) { error.value = String(e.message || e); }
      timer = setInterval(tick, 4000);
    });
    onUnmounted(() => timer && clearInterval(timer));

    const fmtMs = (ms) => (ms >= 1000 ? (ms / 1000).toFixed(1) + "s" : ms + "ms");

    return {
      runs, current, topic, note, busy, error, done, activeStep,
      STEPS, badge, fmtMs, openRun, createRun, selectTopic, review, loadRuns,
    };
  },

  template: `
<div>
  <aside>
    <div class="brand">Content<span>Ops</span> · 自媒体运营流水线</div>
    <div class="sub">选题 → 人工选 → 写稿 → 人工审 → 提炼图点 → 并行出图</div>

    <div class="col" style="margin-bottom:18px">
      <input type="text" v-model="topic" placeholder="选题方向，例如：AI 编程入门"
             @keyup.enter="createRun" :disabled="busy">
      <button @click="createRun" :disabled="busy || !topic.trim()">
        {{ busy ? '跑着呢…' : '跑一条' }}
      </button>
    </div>

    <h2 style="margin-top:8px">全部运行 · {{ runs.length }}</h2>
    <div class="col">
      <div v-for="r in runs" :key="r.run_id"
           class="run" :class="{active: current && current.run.run_id === r.run_id}"
           @click="openRun(r.run_id)">
        <div class="t">{{ r.topic }}</div>
        <div class="m">
          <span class="badge" :class="badge(r.status)[0]">{{ badge(r.status)[1] }}</span>
          <span>第 {{ r.revision }} 版</span>
          <span>{{ r.updated_at.slice(5, 16).replace('T', ' ') }}</span>
        </div>
      </div>
      <div v-if="!runs.length" class="dim" style="font-size:12.5px">还没有运行记录</div>
    </div>
  </aside>

  <main>
    <div v-if="!current" class="empty">
      <div style="font-size:15px">左边填个方向，跑第一条</div>
      <div class="sub" style="margin-top:8px">跑到「人选选题」会停下，点一个再往下走</div>
    </div>

    <template v-else>
      <div class="row" style="justify-content:space-between">
        <h1>{{ current.state.selected_topic || current.state.topic_direction || current.run.topic }}</h1>
        <span class="badge" :class="badge(current.run.status)[0]">{{ badge(current.run.status)[1] }}</span>
      </div>

      <div class="steps">
        <span v-for="[key, label] in STEPS" :key="key"
              class="step" :class="{on: activeStep === key, done: done.has(key)}">{{ label }}</span>
      </div>

      <div class="card">
        <div class="meta">
          <div><b>版本</b>第 {{ current.run.revision }} 版</div>
          <div><b>模型</b><span class="mono">{{ current.run.mode || '—' }}</span></div>
          <div><b>事件</b>{{ current.events.length }} 条</div>
          <div><b>运行 id</b><span class="mono">{{ current.run.run_id }}</span></div>
        </div>
      </div>

      <!-- 第 1 步：选题 -->
      <template v-if="current.step === 'select_topic'">
        <h2>第 1 步 · 选一个选题（AI 出了 {{ (current.packet.options || []).length }} 个）</h2>
        <div class="card">
          <div class="options">
            <div v-for="(opt, i) in current.packet.options" :key="opt"
                 class="opt" @click="selectTopic(opt)">
              <span class="idx">{{ i + 1 }}</span>
              <span>{{ opt }}</span>
            </div>
          </div>
        </div>
      </template>

      <!-- 第 2 步：审稿 -->
      <template v-else-if="current.step === 'review'">
        <h2>第 2 步 · 人工审核（第 {{ current.packet.revision }} 版）</h2>
        <div class="card">
          <pre>{{ current.packet.draft }}</pre>
          <textarea v-model="note" placeholder="打回意见（放行可留空）" style="margin-top:14px"></textarea>
          <div class="row" style="margin-top:10px">
            <button @click="review('approve')" :disabled="busy">放行，去出图</button>
            <button class="danger" @click="review('reject')" :disabled="busy">打回重写</button>
          </div>
        </div>
      </template>

      <!-- 成品 -->
      <template v-else>
        <template v-if="current.state.article_content">
          <h2>正文 · 第 {{ current.state.revision }} 版</h2>
          <div class="card"><pre>{{ current.state.article_content }}</pre></div>
        </template>

        <template v-if="(current.state.visual_points || []).length">
          <h2>配图脚本 · {{ current.state.visual_points.length }} 个图点</h2>
          <div class="card">
            <div class="col">
              <div v-for="(p, i) in current.state.visual_points" :key="i">
                <span class="dim mono">{{ i + 1 }}.</span> {{ p }}
              </div>
            </div>
          </div>
        </template>

        <template v-if="(current.state.image_urls || []).length">
          <h2>配图 · {{ current.state.image_urls.length }} 张</h2>
          <div class="card">
            <div class="grid">
              <figure v-for="(u, i) in current.state.image_urls" :key="u">
                <img :src="u" :alt="'图 ' + (i + 1)" loading="lazy">
                <figcaption>{{ i + 1 }}. {{ (current.state.visual_points || [])[i] || '' }}</figcaption>
              </figure>
            </div>
          </div>
        </template>

        <template v-if="(current.state.image_errors || []).length">
          <h2>出图失败</h2>
          <div class="card">
            <div class="col">
              <div v-for="e in current.state.image_errors" :key="e" class="bad">· {{ e }}</div>
            </div>
          </div>
        </template>

        <div v-if="current.run.status === 'ended'" class="card" style="margin-top:18px">
          <span class="dim">打回次数用尽，没有出图 —— 这一版不算交付。</span>
        </div>
      </template>

      <!-- 观测 -->
      <h2>trace · {{ current.events.length }} 条</h2>
      <div class="card">
        <table>
          <tr><th>#</th><th>节点</th><th>耗时</th><th>说明</th></tr>
          <tr v-for="(e, i) in current.events" :key="i">
            <td class="dim mono">{{ i + 1 }}</td>
            <td class="mono">{{ e.node }}</td>
            <td class="mono">{{ fmtMs(e.ms) }}</td>
            <td>{{ e.note }}</td>
          </tr>
        </table>
      </div>

      <template v-if="(current.state.node_metrics || []).length">
        <h2>节点成本</h2>
        <div class="card">
          <table>
            <tr><th>节点</th><th>耗时</th><th>token</th><th>模型</th></tr>
            <tr v-for="m in current.state.node_metrics" :key="m.node + m.ms">
              <td class="mono">{{ m.node }}</td>
              <td class="mono">{{ fmtMs(m.ms) }}</td>
              <td class="mono">{{ m.tokens || '—' }}</td>
              <td class="mono dim">{{ m.model || '—' }}</td>
            </tr>
          </table>
        </div>
      </template>
    </template>

    <div v-if="error" class="toast" @click="error = ''">{{ error }}（点我关掉）</div>
  </main>
</div>
  `,
}).mount("#app");
