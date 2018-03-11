from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

# Basic imports
import os,sys,time
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np

# Import more libraries (after configuration is validated)
import tensorflow as tf
from uresnet import uresnet
from larcv import larcv
from larcv.dataloader2 import larcv_threadio
from config import ssnet_config

class ssnet_trainval(object):

  def __init__(self):
    self._cfg = ssnet_config()
    self._filler = None
    self._drainer = None
    self._iteration = -1
    # SUCK IT
    self._freeze_all = self._cfg.FREEZE_BASE and self._cfg.FREEZE_CLASS
    if self._cfg.PREDICT_VERTEX:
      self._freeze_all = self._freeze_all and self._cfg.FREEZE_VERTEX

  def __del__(self):
    try:
      if self._filler:
        self._filler.reset()
    except AttributeError:
      pass
    try:
      if self._drainer:
        self._drainer.finalize()
    except AttributeError:
      pass

  def iteration_from_file_name(self,file_name):
    return int((file_name.split('-'))[-1])

  def override_config(self,file_name):
    self._cfg.override(file_name)
    self._cfg.dump()
    # SUCK IT
    self._freeze_all = self._cfg.FREEZE_BASE and self._cfg.FREEZE_CLASS
    if self._cfg.PREDICT_VERTEX:
      self._freeze_all = self._freeze_all and self._cfg.FREEZE_VERTEX

  def initialize(self):
    # Instantiate and configure
    if not self._cfg.FILLER_CONFIG:
      print('Must provide larcv data filler configuration file!')
      return

    self._filler = larcv_threadio()
    filler_cfg = {'filler_name' : 'ThreadProcessor',
                  'verbosity'   : 0, 
                  'filler_cfg'  : self._cfg.FILLER_CONFIG}
    self._filler.configure(filler_cfg)
    # Start IO thread
    self._filler.start_manager(self._cfg.MINIBATCH_SIZE)
    # If requested, construct an output stream
    if self._cfg.DRAINER_CONFIG:
      self._drainer = larcv.IOManager(self._cfg.DRAINER_CONFIG)
      self._drainer.initialize()

    # Retrieve image/label dimensions
    self._filler.next(store_entries   = self._freeze_all,
                      store_event_ids = self._freeze_all)
    dim_data = self._filler.fetch_data(self._cfg.KEYWORD_DATA).dim()
    #dims = []
    self._net = uresnet(dims = dim_data[1:],
                        num_class = self._cfg.NUM_CLASS, 
                        base_num_outputs = self._cfg.BASE_NUM_FILTERS, 
                        debug = False)

    # define freeze-layer config
    freeze = (self._cfg.FREEZE_BASE, self._cfg.FREEZE_CLASS, self._cfg.FREEZE_VERTEX)
    self._net.construct(freeze         = freeze,
                        use_weight     = self._cfg.USE_WEIGHTS,
                        learning_rate  = self._cfg.LEARNING_RATE,
                        predict_vertex = self._cfg.PREDICT_VERTEX)

    self._iteration = 0

  def _report(self,step,metrics,descr):
    msg = 'Training in progress @ step %-4d ... ' % step
    for i,desc in enumerate(descr):
      if not desc: continue
      msg += '%s=%6.6f   ' % (desc,metrics[i])
    msg += '\n'
    sys.stdout.write(msg)
    sys.stdout.flush()

  def run(self,sess):
    # Set random seed for reproducibility
    tf.set_random_seed(1234)
    # Configure global process (session, summary, etc.)
    # Create a bandle of summary
    merged_summary=tf.summary.merge_all()
    # Initialize variables
    sess.run(tf.global_variables_initializer())
    writer = None
    if self._cfg.LOGDIR:
      # Create a summary writer handle
      writer=tf.summary.FileWriter(self._cfg.LOGDIR)
      writer.add_graph(sess.graph)
    saver = None
    if self._cfg.SAVE_FILE:
      # Create weights saver
      saver = tf.train.Saver()
      
    # Override variables if wished
    if self._cfg.LOAD_FILE:
      vlist=[]
      self._iteration = self.iteration_from_file_name(self._cfg.LOAD_FILE)
      parent_vlist = tf.get_collection(tf.GraphKeys.MODEL_VARIABLES)
      for v in parent_vlist:
        if v.name in self._cfg.AVOID_LOAD_PARAMS:
          print('\033[91mSkipping\033[00m loading variable',v.name,'from input weight...')
          continue
        print('\033[95mLoading\033[00m variable',v.name,'from',self._cfg.LOAD_FILE)
        vlist.append(v)
      for v in vlist: print( v)
      reader=tf.train.Saver(var_list=vlist)
      reader.restore(sess,self._cfg.LOAD_FILE)
    
    # Run iterations
    for i in xrange(self._cfg.ITERATIONS):
      if not self._freeze_all and self._iteration >= self._cfg.ITERATIONS:
        print('Finished training (iteration %d)' % self._iteration)
        break

      # Start IO thread for the next batch while we train the network
      if not self._freeze_all:
        batch_metrics = None
        descr_metrics = None
        for j in xrange(self._cfg.NUM_MINIBATCHES):
          self._net.zero_gradients(sess)
          minibatch_data   = self._filler.fetch_data(self._cfg.KEYWORD_DATA).data()
          minibatch_class_label   = self._filler.fetch_data(self._cfg.KEYWORD_CLASS_LABEL).data()
          minibatch_class_weight  = None
          minibatch_vertex_label  = None
          minibatch_vertex_weight = None
          if self._cfg.USE_WEIGHTS:
            minibatch_class_weight = self._filler.fetch_data(self._cfg.KEYWORD_CLASS_WEIGHT).data()
            # perform per-event normalization
            #print(np.sum(minibatch_class_weight,axis=1))
            minibatch_class_weight /= (np.sum(minibatch_class_weight,axis=1).reshape([minibatch_class_weight.shape[0],1]))
            #print( (minibatch_class_weight[0] > 0.).astype(np.int32).sum() )
          if self._cfg.PREDICT_VERTEX:
            minibatch_vertex_label  = self._filler.fetch_data(self._cfg.KEYWORD_VERTEX_LABEL ).data()
            minibatch_vertex_weight = self._filler.fetch_data(self._cfg.KEYWORD_VERTEX_WEIGHT).data()
            # perform per-event normalization
            #print(np.sum(minibatch_vertex_weight,axis=1))
            minibatch_vertex_weight /= (np.sum(minibatch_vertex_weight,axis=1).reshape([minibatch_vertex_weight.shape[0],1]))
            #print( (minibatch_vertex_weight[0] > 0.).astype(np.int32).sum() )
          res,doc = self._net.accum_gradients(sess                = sess,
                                              input_data          = minibatch_data,
                                              input_class_label   = minibatch_class_label,
                                              input_class_weight  = minibatch_class_weight,
                                              input_vertex_label  = minibatch_vertex_label,
                                              input_vertex_weight = minibatch_vertex_weight)
          if batch_metrics is None:
            batch_metrics = np.zeros((self._cfg.NUM_MINIBATCHES,len(res)-1),dtype=np.float32)
            descr_metrics = doc[1:]
            batch_metrics[j,:] = res[1:]

          if (j+1) == self._cfg.NUM_MINIBATCHES and  self._cfg.SUMMARY_STEPS and ((self._iteration+1)%self._cfg.SUMMARY_STEPS) == 0:
          # Run summary                                                                                                                                                             
            feed_dict = self._net.feed_dict(input_data          = minibatch_data,
                                            input_class_label   = minibatch_class_label,
                                            input_class_weight  = minibatch_class_weight,
                                            input_vertex_label  = minibatch_vertex_label,
                                            input_vertex_weight = minibatch_vertex_weight)

            writer.add_summary(sess.run(merged_summary,feed_dict=feed_dict),self._iteration)

          self._filler.next(store_entries   = self._freeze_all,
                            store_event_ids = self._freeze_all)
        #update
        self._net.apply_gradients(sess)
        self._iteration += 1
        self._report(self._iteration,np.mean(batch_metrics,axis=0),descr_metrics)

        sys.stdout.flush()

        '''
        # Save log
        if self._cfg.SUMMARY_STEPS and ((self._iteration+1)%self._cfg.SUMMARY_STEPS) == 0:
          # Run summary
          feed_dict = self._net.feed_dict(input_data          = minibatch_data,
                                          input_class_label   = minibatch_class_label,
                                          input_class_weight  = minibatch_class_weight,
                                          input_vertex_label  = minibatch_vertex_label,
                                          input_vertex_weight = minibatch_vertex_weight)
                                        
          writer.add_summary(sess.run(merged_summary,feed_dict=feed_dict),self._iteration)
        '''
        # Save snapshot
        if self._cfg.CHECKPOINT_STEPS and ((self._iteration+1)%self._cfg.CHECKPOINT_STEPS) == 0:
          # Save snapshot
          ssf_path = saver.save(sess,self._cfg.SAVE_FILE,global_step=self._iteration)
          print()
          print('saved @',ssf_path)

        #self._filler.next(store_entries   = self._freeze_all, store_event_ids = self._freeze_all)     

      else:
        # Receive data (this will hang if IO thread is still running = this will wait for thread to finish & receive data)

        batch_data   = self._filler.fetch_data(self._cfg.KEYWORD_DATA).data()
        batch_class_label   = self._filler.fetch_data(self._cfg.KEYWORD_CLASS_LABEL).data()
        batch_class_weight  = None
        batch_vertex_weight = None

        softmax,acc_all,acc_nonzero = self._net.inference(sess        = sess,
                                                          input_data  = batch_data,
                                                          input_class_label = batch_class_label)
        print('Inference accuracy:', acc_all, '/', acc_nonzero)

        if self._drainer:
          for entry in xrange(len(softmax)):
            self._drainer.read_entry(entry)
            data  = np.array(batch_data[entry]).reshape(softmax.shape[1:-1])
          entries   = self._filler.fetch_entries()
          event_ids = self._filler.fetch_event_ids()

          for entry in xrange(len(softmax)):

            self._drainer.read_entry(entries[entry])
            data  = np.array(batch_data[entry]).reshape(softmax.shape[1:-1])
            label = np.array(batch_class_label[entry]).reshape(softmax.shape[1:-1])          
            shower_score = softmax[entry,:,:,:,1]
            track_score  = softmax[entry,:,:,:,2]
            
            sum_score = shower_score + track_score
            shower_score = shower_score / sum_score
            track_score  = track_score  / sum_score
            
            ssnet_result = (shower_score > track_score).astype(np.float32) + (track_score >= shower_score).astype(np.float32) * 2.0
            nonzero_map = (data > 1.0).astype(np.int32)
            ssnet_result = (ssnet_result * nonzero_map).astype(np.float32)
            #print(ssnet_result.shape,ssnet_result.max(),ssnet_result.min(),(ssnet_result<1).astype(np.int32).sum())
            #print(larcv.as_tensor3d(ssnet_result))

            data = self._drainer.get_data("sparse3d","data")
            sparse3d = self._drainer.get_data("sparse3d","ssnet")
            vs = larcv.as_tensor3d(ssnet_result)
            #sparse3d = vs
            #print( vs.as_vector().size())
            #for vs_index in xrange(vs.as_vector().size()):
            #  vox = vs.as_vector()[vs_index]
            #  sparse3d.add(vs.as_vector()[vs_index])
            sparse3d.set(vs,data.meta())
            #print(data.event_key())
            self._drainer.save_entry()
            #self._drainer.clear_entry()
        
        if self._cfg.DUMP_IMAGE:
          for image_index in xrange(len(softmax)):
            event_image = softmax[image_index]
            bg_image = event_image[:,:,0]
            track_image = event_image[:,:,1]
            shower_image = event_image[:,:,2]
            bg_image_name = 'SOFTMAX_BG_%05d.png' % (i * self._cfg.BATCH_SIZE + image_index)
            track_image_name = 'SOFTMAX_TRACK_%05d.png' % (i * self._cfg.BATCH_SIZE + image_index)
            shower_image_name = 'SOFTMAX_SHOWER_%05d.png' % (i * self._cfg.BATCH_SIZE + image_index)
            
            fig,ax = plt.subplots(figsize=(12,8),facecolor='w')
            plt.imshow((bg_image * 255.).astype(np.uint8),vmin=0,vmax=255,cmap='jet',interpolation='none').write_png(bg_image_name)
            plt.close()

            fig,ax = plt.subplots(figsize=(12,8),facecolor='w')
            plt.imshow((shower_image * 255.).astype(np.uint8),vmin=0,vmax=255,cmap='jet',interpolation='none').write_png(shower_image_name)
            plt.close()
            
            fig,ax = plt.subplots(figsize=(12,8),facecolor='w')
            plt.imshow((track_image * 255.).astype(np.uint8),vmin=0,vmax=255,cmap='jet',interpolation='none').write_png(track_image_name)
            plt.close()

        self._filler.next(store_entries   = self._freeze_all,
                          store_event_ids = self._freeze_all)

    del self._filler
    #self._filler = None
