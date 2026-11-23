// 当前已取到的报告模型(health / topsql / wdr / kb)。视图画完后登记在这里,抽屉、命令面板、智能体侧栏从这里读;
// 换实例或换风格时整体清空。只是一个内存表,不落盘。

const models = {};
export const setModel = (name, m) => { models[name] = m; };
export const getModel = (name) => models[name] || null;
export const clearModels = () => { for (const k of Object.keys(models)) delete models[k]; };
