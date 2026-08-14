<script setup lang="ts">
import { ref } from 'vue'
import SettingsGeneral from './SettingsGeneral.vue'
import SettingsTtl from './SettingsTtl.vue'
import SettingsExtensions from './SettingsExtensions.vue'
import ProvidersView from '../llm/ProvidersView.vue'
import TemplatesView from '../templates/TemplatesView.vue'
import AgentsTab from './AgentsTab.vue'
import RegistryTab from './RegistryTab.vue'
import PromptsTab from './PromptsTab.vue'
import BackupTab from './BackupTab.vue'

// 对齐参考设计：设置页单入口 + 标签页横向切分各配置功能区
const TABS = [
  { key: 'general', label: '通用设置', comp: SettingsGeneral },
  // 供应商与降级链合并为单页（含新增/删除供应商；链/规则仍只读展示）
  { key: 'providers', label: '模型供应商', comp: ProvidersView },
  { key: 'ttl', label: 'TTL 配置', comp: SettingsTtl },
  { key: 'templates', label: '模板管理', comp: TemplatesView },
  { key: 'agents', label: 'Agent 管理', comp: AgentsTab },
  { key: 'registry', label: '维度注册表', comp: RegistryTab },
  { key: 'prompts', label: '提示词', comp: PromptsTab },
  { key: 'extensions', label: '扩展模块', comp: SettingsExtensions },
  { key: 'backup', label: '备份管理', comp: BackupTab },
]
const active = ref('general')
</script>

<template>
  <div class="settings">
    <div class="head">
      <h2>设置</h2>
      <span class="sub">配置供应商 / 降级链 / TTL / 模板 / 扩展模块 / 备份</span>
    </div>

    <div class="tabs">
      <button
        v-for="t in TABS"
        :key="t.key"
        :class="{ active: active === t.key }"
        @click="active = t.key"
      >
        {{ t.label }}
      </button>
    </div>

    <div class="tab-body">
      <component
        :is="TABS.find((t) => t.key === active)!.comp"
        v-bind="TABS.find((t) => t.key === active)!.props || {}"
      />
    </div>
  </div>
</template>

<style scoped>
.tab-body { padding-top: 4px; }
</style>