# FYP-DrugDiscoveryWithDeepLearning

Conventional drug design and discovery is a time-consuming and computationally expensive task considering the comprehensive structure of the target protein and the necessity of generating chemically valid molecules with optimal properties. Recently, the advancement in computational power and machine learning approaches has accelerated this task by providing the more efficient algorithms for handling the available data. However, discovering the de novo molecular structure remains to be a challenge. The blossom of Generative Adversarial Networks (GANs), which have obtained outstanding performance on generative tasks such as image and video synthesis, brings a brand-new solution for this topic. The aim of this project is to develop a GAN model that design the drug compound with the guidance of gene mutation data, which has been demonstrated to have a strong impact on cancer progression in numerous literatures. 

The model will be developed in two stages: (1) an encoder-decoder model will be trained to learn the latent representation of a drug using SMILES vectors as input; (2) the generator and the discriminator will be trained simultaneously to generate drug-like molecules. Notably, gene mutation profile will be used as input in the second stage to formulate desired drugs. In the end, the synthetic molecules will be examined by chemicals rules to ensure they are valid and possessed with optimal chemical properties.