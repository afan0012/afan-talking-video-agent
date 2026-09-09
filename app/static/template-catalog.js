// Static packaging catalogue.  Interaction and persistence stay in app.js;
// keeping the data separate prevents visual template edits from touching the
// workflow controller.
window.TEMPLATE_CATALOG = {
  storageKey: 'talkforge_packaging_templates_v1',
  builtins: {
    title: [
      { id: 'title-classic', name: '顶部白字', fields: { title_font_size: 'h/18', title_color: 'white', title_position: 'top' } },
      { id: 'title-impact', name: '中心强调', fields: { title_font_size: 'h/14', title_color: 'yellow', title_position: 'center' } },
      { id: 'title-bottom', name: '底部提示', fields: { title_font_size: 'h/24', title_color: '#4ECDC4', title_position: 'bottom' } },
    ],
    subtitle: [
      { id: 'subtitle-inline-cyan', name: '青色行内强调', description: '上方白青强调 + 下方白色口播', preview: '/static/template-previews/subtitle-inline-cyan.svg', fields: { subtitle_style: 'inline-cyan', title_font_size: 'h/18', title_color: 'white', title_position: 'top', subtitle_enabled: true, subtitle_font_size: '42', subtitle_color: 'FFFFFF', subtitle_margin_v: '88', subtitle_keyword_color: '4ECDC4' } },
      { id: 'subtitle-classic-yellow', name: '经典黄词描边', description: '上方红白标题 + 下方黄词高亮', preview: '/static/template-previews/subtitle-classic-yellow.svg', fields: { subtitle_style: 'classic-yellow', title_font_size: 'h/18', title_color: 'white', title_position: 'top', subtitle_enabled: true, subtitle_font_size: '42', subtitle_color: 'FFFFFF', subtitle_margin_v: '78', subtitle_keyword_color: 'FFFF00' } },
      { id: 'subtitle-highlight-red', name: '重点红字', description: '上方白黄强调 + 下方红色关键词', preview: '/static/template-previews/subtitle-highlight-red.svg', fields: { subtitle_style: 'highlight-red', title_font_size: 'h/18', title_color: 'white', title_position: 'top', subtitle_enabled: true, subtitle_font_size: '48', subtitle_color: 'FFFFFF', subtitle_margin_v: '84', subtitle_keyword_color: 'FF6B6B' } },
      { id: 'subtitle-soft-white', name: '柔和白字', description: '上下白字描边 + 轻量关键词色彩', preview: '/static/template-previews/subtitle-soft-white.svg', fields: { subtitle_style: 'soft-white', title_font_size: 'h/24', title_color: 'white', title_position: 'top', subtitle_enabled: true, subtitle_font_size: '38', subtitle_color: 'FFFFFF', subtitle_margin_v: '104', subtitle_keyword_color: 'FF7C83' } },
    ],
    cover: [
      { id: 'cover-diagonal-yellow', name: '黄带大字', description: '全屏人物 + 黄带主标题', preview: '/static/template-previews/cover-diagonal-yellow.svg', fields: { cover_style: 'diagonal-yellow' } },
      { id: 'cover-giant-headline', name: '超大标题', description: '顶部大字 + 底部副标题条', preview: '/static/template-previews/cover-giant-headline.svg', fields: { cover_style: 'giant-headline' } },
      { id: 'cover-vertical-cutout', name: '竖排聚焦', description: '背景模糊 + 右侧竖排大字（不自动抠图）', preview: '/static/template-previews/cover-vertical-focus.svg', fields: { cover_style: 'vertical-cutout' } },
      { id: 'cover-center-sticker', name: '居中贴纸', description: '居中主标题 + 深色副标题区', preview: '/static/template-previews/cover-center-sticker.svg', fields: { cover_style: 'center-sticker' } },
    ],
  },
  fields: {
    title: ['title_font_size', 'title_color', 'title_position'],
    subtitle: ['subtitle_style', 'title_font_size', 'title_color', 'title_position', 'subtitle_enabled', 'subtitle_font_size', 'subtitle_color', 'subtitle_margin_v', 'subtitle_keyword_color'],
    cover: ['cover_style'],
  },
};
