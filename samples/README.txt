样例内容
========
repo.git/                       裸仓库：main=dd761d41923dd207f757b9056a9f68899b2e50ab，stable=5be969d645b1832d8346a611f2d3000f97c21eb8
bundles/01-main-c4-feature-f1   main->536e380e0cdaa630640a4c049a61ca87630c0b77, feature->bcd4a734720db4bed50837b471443dd4a4fc09d3, 标签 v1.0（前置 dd761d41923dd207f757b9056a9f68899b2e50ab，仓库已有）
bundles/02-main-c5              main->91e12f6c2e270c9ebfe0623d5479d33197a1ec4e（前置 536e380e0cdaa630640a4c049a61ca87630c0b77，由 01 提供）
bundles/03-feature-f2           feature->b948d8438861679d35dfab5401eb86125496a0da（前置 bcd4a734720db4bed50837b471443dd4a4fc09d3，由 01 提供）
bad/missing-prereq-main-c7      main->d81c5b4ae1c2f65729a8bb0569605fdbf5e59669（前置 13539cef2cdf12078fd70f5605e76deaae608c0a，缺失 → 验包失败并指出该提交）
bad/diverged-main               main->b0c58c2385a279df4212445aa0867e1cfc9a30e4（与 01/02 的 main 分叉 → 整批退回）

正常顺序：01 -> 02 -> 03（可故意乱序上传验证自动排序）。
导入 01+02+03 后：main=91e12f6c2e270c9ebfe0623d5479d33197a1ec4e，feature=b948d8438861679d35dfab5401eb86125496a0da，stable 不变。
